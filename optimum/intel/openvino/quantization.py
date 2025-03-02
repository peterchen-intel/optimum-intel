#  Copyright 2022 The HuggingFace Team. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import inspect
import io
import logging
from itertools import chain
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

import nncf
import openvino
import torch
import transformers
from datasets import Dataset, load_dataset
from huggingface_hub import HfApi
from nncf import NNCFConfig
from nncf.torch import create_compressed_model, register_default_init_args
from nncf.torch.dynamic_graph.io_handling import wrap_nncf_model_inputs_with_objwalk
from nncf.torch.initialization import PTInitializingDataLoader
from nncf.torch.nncf_network import NNCFNetwork
from openvino._offline_transformations import compress_quantize_weights_transformation
from openvino.runtime import Core, Tensor
from torch.onnx import export as onnx_export
from torch.utils._pytree import tree_map
from torch.utils.data import DataLoader, RandomSampler
from transformers import DataCollator, PreTrainedModel, default_data_collator

from optimum.exporters import TasksManager
from optimum.exporters.onnx import OnnxConfig
from optimum.quantization_base import OptimumQuantizer

from ..utils.constant import _TASK_ALIASES
from .configuration import OVConfig
from .modeling_base import OVBaseModel
from .modeling_decoder import OVBaseDecoderModel
from .utils import (
    MAX_ONNX_OPSET,
    MIN_ONNX_QDQ_OPSET,
    ONNX_WEIGHTS_NAME,
    OV_XML_FILE_NAME,
    use_external_data_format,
)


core = Core()
logger = logging.getLogger(__name__)


class OVDataLoader(PTInitializingDataLoader):
    def get_inputs(self, dataloader_output) -> Tuple[Tuple, Dict]:
        return (), dataloader_output


class OVQuantizer(OptimumQuantizer):
    """
    Handle the NNCF quantization process.
    """

    def __init__(self, model: transformers.PreTrainedModel, task: Optional[str] = None, seed: int = 42, **kwargs):
        """
        Args:
            model (`transformers.PreTrainedModel`):
                The [PreTrainedModel](https://huggingface.co/docs/transformers/main_classes/model#transformers.PreTrainedModel) to quantize.
            task (`str`, defaults to None):
                The task defining the model topology used for the ONNX export.
            seed (`int`, defaults to 42):
                The random seed to use when shuffling the calibration dataset.
        """
        super().__init__()
        self.model = model
        feature = kwargs.pop("feature", None)
        if feature is not None:
            logger.warning("`feature` is deprecated and will be removed in a future version. Use `task` instead.")
            if task is not None and task != feature:
                logger.warning(
                    f"Both `feature` and `task` were specified. {task} will be used to define the model topology for the model ONNX export."
                )
        self.task = task or feature
        self.seed = seed
        self.input_names = None
        signature = inspect.signature(self.model.forward)
        self._signature_columns = list(signature.parameters.keys())
        self._export_input_names = [
            column for column in self._signature_columns if column not in {"label", "labels", "label_ids"}
        ]

    @classmethod
    def from_pretrained(cls, model: PreTrainedModel, **kwargs):
        # TODO : Create model
        return cls(model, **kwargs)

    def quantize(
        self,
        calibration_dataset: Dataset,
        save_directory: Union[str, Path],
        quantization_config: OVConfig = None,
        file_name: Optional[str] = None,
        batch_size: int = 1,
        data_collator: Optional[DataCollator] = None,
        remove_unused_columns: bool = True,
        **kwargs,
    ):
        """
        Quantize a model given the optimization specifications defined in `quantization_config`.

        Args:
            calibration_dataset (`datasets.Dataset`):
                The dataset to use for the calibration step.
            save_directory (`Union[str, Path]`):
                The directory where the quantized model should be saved.
            quantization_config (`OVConfig`, *optional*):
                The configuration containing the parameters related to quantization.
            file_name (`str`, *optional*):
                The model file name to use when saving the model. Overwrites the default file name `"model.onnx"`.
            batch_size (`int`, defaults to 8):
                The number of calibration samples to load per batch.
            data_collator (`DataCollator`, *optional*):
                The function to use to form a batch from a list of elements of the calibration dataset.
            remove_unused_columns (`bool`, defaults to `True`):
                Whether or not to remove the columns unused by the model forward method.

        Example:
        ```python
        >>> from optimum.intel.openvino import OVQuantizer, OVModelForSequenceClassification
        >>> from transformers import AutoModelForSequenceClassification
        >>> model = OVModelForSequenceClassification.from_pretrained("distilbert-base-uncased-finetuned-sst-2-english", export=True)
        >>> # or
        >>> model = AutoModelForSequenceClassification.from_pretrained("distilbert-base-uncased-finetuned-sst-2-english")
        >>> OVQuantizer.from_pretrained(model, task="text-classification")
        >>> quantizer.quantize(calibration_dataset=calibration_dataset, save_directory="./quantized_model")
        >>> optimized_model = OVModelForSequenceClassification.from_pretrained("./quantized_model")
        ```
        """
        if isinstance(self.model, OVBaseDecoderModel) and self.model.use_cache:
            self._quantize_ovcausallm(
                calibration_dataset,
                save_directory,
                batch_size,
                data_collator,
                remove_unused_columns,
                **kwargs,
            )
        elif isinstance(self.model, OVBaseModel):
            self._quantize_ovbasemodel(
                calibration_dataset,
                save_directory,
                batch_size,
                data_collator,
                remove_unused_columns,
                **kwargs,
            )
        elif isinstance(self.model, torch.nn.Module):
            self._quantize_torchmodel(
                calibration_dataset,
                save_directory,
                quantization_config,
                file_name,
                batch_size,
                data_collator,
                remove_unused_columns,
            )
        else:
            raise TypeError(f"Unsupported model type: {type(self.model)}")

    def _quantize_ovbasemodel(
        self,
        calibration_dataset: Dataset,
        save_directory: Union[str, Path],
        batch_size: int = 1,
        data_collator: Optional[DataCollator] = None,
        remove_unused_columns: bool = True,
        **kwargs,
    ):
        save_directory = Path(save_directory)
        save_directory.mkdir(parents=True, exist_ok=True)

        calibration_dataloader = self._get_calibration_dataloader(
            calibration_dataset=calibration_dataset,
            batch_size=batch_size,
            remove_unused_columns=remove_unused_columns,
            data_collator=data_collator,
        )

        quantization_dataset = nncf.Dataset(calibration_dataloader, lambda x: x)
        quantized_model = nncf.quantize(
            self.model.model,
            quantization_dataset,
            model_type=nncf.ModelType.TRANSFORMER if not kwargs.get("model_type") else kwargs.get("model_type"),
            fast_bias_correction=kwargs.get("fast_bias_correction", True),
            subset_size=300 if not kwargs.get("subset_size") else kwargs.get("subset_size"),
            **kwargs,
        )
        self.model.model = quantized_model
        self.model.save_pretrained(save_directory)

    def _quantize_ovcausallm(
        self,
        calibration_dataset: Dataset,
        save_directory: Union[str, Path],
        batch_size: int = 1,
        data_collator: Optional[DataCollator] = None,
        remove_unused_columns: bool = True,
        **kwargs,
    ):
        save_directory = Path(save_directory)
        save_directory.mkdir(parents=True, exist_ok=True)

        calibration_dataloader = self._get_calibration_dataloader(
            calibration_dataset=calibration_dataset,
            batch_size=batch_size,
            remove_unused_columns=remove_unused_columns,
            data_collator=data_collator,
        )

        # Prefeth past_key_values
        self.model.compile()
        subset_size = kwargs.get("subset_size", 300)
        data_cache = []

        class InferRequestWrapper:
            def __init__(self, request):
                self.request = request

            def __call__(self, *args, **kwargs):
                data_cache.append(*args)
                return self.request(*args, *kwargs)

            def infer(self, inputs: Any = None, shared_memory: bool = False):
                data_cache.append(inputs)
                return self.request.infer(inputs, shared_memory)

            def start_async(
                self,
                inputs: Any = None,
                userdata: Any = None,
                shared_memory: bool = False,
            ):
                data_cache.append(inputs)
                self.request.infer(inputs, shared_memory)

            def wait(self):
                pass

            def get_tensor(self, name: str):
                return Tensor(self.request.results[name])

            def __getattr__(self, attr):
                if attr in self.__dict__:
                    return getattr(self, attr)
                return getattr(self.request, attr)

        self.model.request = InferRequestWrapper(self.model.request)
        for _, data in enumerate(calibration_dataloader):
            self.model.generate(**data, max_new_tokens=100)
            if len(data_cache) >= subset_size:
                break
        self.model.request = self.model.request.request

        # Actual model quantization
        quantization_dataset = nncf.Dataset(data_cache, lambda x: x)
        quantized_model = nncf.quantize(
            self.model.model,
            quantization_dataset,
            model_type=nncf.ModelType.TRANSFORMER if not kwargs.get("model_type") else kwargs.get("model_type"),
            fast_bias_correction=True
            if not kwargs.get("fast_bias_correction")
            else kwargs.get("fast_bias_correction"),
            subset_size=subset_size,
            **kwargs,
        )
        self.model.model = quantized_model
        self.model.save_pretrained(save_directory)

    def _quantize_torchmodel(
        self,
        calibration_dataset: Dataset,
        save_directory: Union[str, Path],
        quantization_config: OVConfig = None,
        file_name: Optional[str] = None,
        batch_size: int = 1,
        data_collator: Optional[DataCollator] = None,
        remove_unused_columns: bool = True,
    ):
        save_directory = Path(save_directory)
        save_directory.mkdir(parents=True, exist_ok=True)
        file_name = file_name if file_name is not None else OV_XML_FILE_NAME
        output_path = save_directory.joinpath(file_name)
        output_path = output_path.with_suffix(".xml").as_posix()

        calibration_dataloader = self._get_calibration_dataloader(
            calibration_dataset=calibration_dataset,
            batch_size=batch_size,
            remove_unused_columns=remove_unused_columns,
            data_collator=data_collator,
        )
        model_inputs = next(iter(calibration_dataloader))
        if quantization_config is None:
            logger.info(
                "No configuration describing the quantization process was provided, a default OVConfig will be generated."
            )
            quantization_config = OVConfig()
        quantization_config.add_input_info(model_inputs)
        nncf_config = NNCFConfig.from_dict(quantization_config.__dict__)
        nncf_config = register_default_init_args(nncf_config, calibration_dataloader)
        controller, compressed_model = create_compressed_model(
            self.model, nncf_config, wrap_inputs_fn=wrap_nncf_model_inputs_with_objwalk
        )
        controller.prepare_for_export()

        self._set_task()

        self.model.config.save_pretrained(save_directory)
        model_type = self.model.config.model_type.replace("_", "-")
        onnx_config_class = TasksManager.get_exporter_config_constructor(
            exporter="onnx",
            model=self.model,
            task=self.task,
            model_type=model_type,
        )

        if self.task == "text-generation":
            onnx_config = onnx_config_class(self.model.config, use_past=self.model.config.use_cache)
        else:
            onnx_config = onnx_config_class(self.model.config)

        compressed_model.eval()
        num_parameters = compressed_model.num_parameters()
        save_as_external_data = use_external_data_format(num_parameters) or quantization_config.save_onnx_model
        f = io.BytesIO() if not save_as_external_data else save_directory / ONNX_WEIGHTS_NAME

        # Export the compressed model to the ONNX format
        opset = min(onnx_config.DEFAULT_ONNX_OPSET, MAX_ONNX_OPSET)
        opset = opset if not quantization_config.save_onnx_model else max(opset, MIN_ONNX_QDQ_OPSET)
        _onnx_export_nncf_model(compressed_model, onnx_config, f, opset)

        # Load and save the compressed model
        model = core.read_model(f) if save_as_external_data else core.read_model(f.getvalue(), b"")
        self._save_pretrained(model, output_path)
        quantization_config.save_pretrained(save_directory)

    @staticmethod
    def _save_pretrained(model: openvino.runtime.Model, output_path: str):
        compress_quantize_weights_transformation(model)
        openvino.runtime.serialize(model, output_path, output_path.replace(".xml", ".bin"))

    def _set_task(self):
        if self.task is None:
            self.task = HfApi().model_info(self.model.config._name_or_path).pipeline_tag
            if self.task is None:
                raise ValueError(
                    "The task defining the model topology could not be extracted and needs to be specified for the ONNX export."
                )

        self.task = _TASK_ALIASES.get(self.task, self.task)

        if self.task == "text2text-generation":
            raise ValueError("Seq2Seq models are currently not supported for post-training static quantization.")

    def get_calibration_dataset(
        self,
        dataset_name: str,
        num_samples: int = 100,
        dataset_config_name: Optional[str] = None,
        dataset_split: str = "train",
        preprocess_function: Optional[Callable] = None,
        preprocess_batch: bool = True,
        use_auth_token: bool = False,
        cache_dir: Optional[str] = None,
    ) -> Dataset:
        """
        Create the calibration `datasets.Dataset` to use for the post-training static quantization calibration step.

        Args:
            dataset_name (`str`):
                The dataset repository name on the Hugging Face Hub or path to a local directory containing data files
                in generic formats and optionally a dataset script, if it requires some code to read the data files.
            num_samples (`int`, defaults to 100):
                The maximum number of samples composing the calibration dataset.
            dataset_config_name (`str`, *optional*):
                The name of the dataset configuration.
            dataset_split (`str`, defaults to `"train"`):
                Which split of the dataset to use to perform the calibration step.
            preprocess_function (`Callable`, *optional*):
                Processing function to apply to each example after loading dataset.
            preprocess_batch (`bool`, defaults to `True`):
                Whether the `preprocess_function` should be batched.
            use_auth_token (`bool`, defaults to `False`):
                Whether to use the token generated when running `transformers-cli login`.
            cache_dir (`str`, *optional*):
                Caching directory for a calibration dataset.
        Returns:
            The calibration `datasets.Dataset` to use for the post-training static quantization calibration step.
        """
        calibration_dataset = load_dataset(
            dataset_name,
            name=dataset_config_name,
            split=dataset_split,
            use_auth_token=use_auth_token,
            cache_dir=cache_dir,
        )

        if num_samples is not None:
            num_samples = min(num_samples, len(calibration_dataset))
            calibration_dataset = calibration_dataset.shuffle(seed=self.seed).select(range(num_samples))

        if preprocess_function is not None:
            calibration_dataset = calibration_dataset.map(preprocess_function, batched=preprocess_batch)

        return calibration_dataset

    def _get_calibration_dataloader(
        self,
        calibration_dataset: Dataset,
        batch_size: int,
        remove_unused_columns: bool,
        data_collator: Optional[DataCollator] = None,
    ) -> OVDataLoader:
        data_collator = data_collator if data_collator is not None else default_data_collator
        if remove_unused_columns:
            calibration_dataset = self._remove_unused_columns(calibration_dataset)
        self.input_names = calibration_dataset.column_names
        generator = torch.Generator()
        generator.manual_seed(self.seed)
        sampler = RandomSampler(calibration_dataset, generator=generator)
        calibration_dataloader = DataLoader(
            calibration_dataset, batch_size=batch_size, sampler=sampler, collate_fn=data_collator, drop_last=False
        )
        return OVDataLoader(calibration_dataloader)

    def _remove_unused_columns(self, dataset: Dataset):
        ignored_columns = list(set(dataset.column_names) - set(self._signature_columns))
        return dataset.remove_columns(ignored_columns)


def _onnx_export_nncf_model(model: NNCFNetwork, config: OnnxConfig, output: Union[str, io.BytesIO], opset: int = None):
    signature = inspect.signature(model.forward)
    signature = list(signature.parameters.keys())
    opset = opset or config.DEFAULT_ONNX_OPSET
    model_inputs = config.generate_dummy_inputs(framework="pt")
    # Create ordered inputs for the ONNX export of NNCFNetwork as keyword arguments are currently not supported
    model_inputs = tuple(model_inputs.pop(key, None) for key in signature if len(model_inputs) != 0)
    device = model.device

    def remap(value):
        if isinstance(value, torch.Tensor):
            value = value.to(device)
        return value

    with config.patch_model_for_export(model):
        model_inputs = tree_map(remap, model_inputs)
        with torch.no_grad():
            model.eval()
            # Disable node additions to be exported in the graph
            model.disable_dynamic_graph_building()
            onnx_export(
                model,
                model_inputs,
                f=output,
                input_names=list(config.inputs.keys()),
                output_names=list(config.outputs.keys()),
                dynamic_axes=dict(chain(config.inputs.items(), config.outputs.items())),
                do_constant_folding=True,
                opset_version=opset,
            )
            model.enable_dynamic_graph_building()
