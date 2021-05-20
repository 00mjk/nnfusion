# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from __future__ import print_function
import os
import copy
import tempfile
import torch
import json
import logging
from .dtypes import str2type
from .utils import cd, execute
from .executor import Executor
from .description import IODescription, ModelDescription
from .data_format import cast_pytorch_tensor

logger = logging.getLogger(__name__)


def tensor2desc(pt_tensor, name=""):
    shape = tuple(pt_tensor.shape)
    dtype = str2type[str(pt_tensor.dtype).split(".")[-1]].type_str
    return IODescription(name, shape, dtype)


def generate_sample(desc, device=None):
    size = [s if isinstance(s, (int)) else 1 for s in desc.shape]
    if desc.num_classes:
        return torch.randint(0,
                             desc.num_classes,
                             size,
                             dtype=str2type[desc.dtype].torch_type).to(device)
    else:
        return torch.ones(size,
                          dtype=str2type[desc.dtype].torch_type).to(device)


def generate_output_desc(model, input_desc, device="cpu"):
    fake_inputs = [generate_sample(desc, device) for desc in input_desc]
    model_copy = copy.deepcopy(model).to(device)
    out = model_copy(*fake_inputs)
    if isinstance(out, torch.Tensor):
        out = (out, )
    return tuple(tensor2desc(t, name=f"output_{i}") for i, t in enumerate(out))


def convert_model_to_onnx(model, model_desc, device, file_name, const_folding):
    model.to(device)
    input_names = [input.name for input in model_desc.inputs]
    output_names = [output.name for output in model_desc.outputs]
    sample_inputs = [
        generate_sample(input, device) for input in model_desc.inputs
    ]
    sample_outputs = [
        generate_sample(output, device) for output in model_desc.outputs
    ]
    # note: onnx exporter might have side effect, so copy a new model
    torch.onnx.export(copy.deepcopy(model).to(device),
                      tuple(sample_inputs),
                      file_name,
                      input_names=input_names,
                      output_names=output_names,
                      opset_version=12,
                      _retain_param_name=True,
                      example_outputs=tuple(sample_outputs),
                      do_constant_folding=const_folding)

    return model


def codegen(model_path, flags, output_dir):
    model_path = os.path.abspath(model_path)
    with cd(output_dir):
        command = "{} {} {}".format("nnfusion", model_path, flags)
        execute(command)


def modify_nnfusion_rt(rt_dir):
    with cd(rt_dir):
        # remove cudaDevice reset in cuda_init()
        command = "sed -i '/cudaDeviceReset()/s:^://:'" + " " + "nnfusion_rt.cu"
        execute(command)


def build(rt_dir):
    with cd(rt_dir):
        command = "cmake ."
        execute([command])

        command = "make -j"
        execute(command)


class PTSession(object):
    """
    A pipeline converting PyTorch model to NNFusion with specific inputs,
    provide a __call__ func to replace the origin model forward.
    """
    def __init__(self,
                 model,
                 input_desc,
                 device,
                 output_desc=None,
                 workdir=None,
                 model_format="onnx",
                 const_folding=False,
                 build_nnf=True,
                 codegen_flags=None,
                 **kwargs):
        """
        Parameters:
            model: torch.nn.Module to be converted.
            input_desc: A list of IODescription representing inputs.
            device: A string representing execution device like "cuda:0",
                currently only tested against cuda device.
            output_desc: Optional, a list of IODescription representing outputs,
                if not provided, the description will be generated by executing PyTorch model.
            workdir: Optional, a string path to generated model & code, if not provided,
                model & code will be stored in a temporary folder, then be cleaned automatically .
            model_format: Intermedia model format, currently only support "onnx".
            const_folding: Do constant folding when converting model to onnx
            build_nnf: build nnf
            codegen_flags: NNFusion codegen flags, 
                ref: https://github.com/microsoft/nnfusion/wiki/4.3-NNFusion-CLI-Interface#cli-flags
        """
        self._model = model
        if model_format != "onnx":
            raise Exception("{} format not supported yet".format(model_format))
        self._model_format = model_format
        self._torch_weights = {
            name: param
            for name, param in self._model.named_parameters()
        }
        self._torch_weights.update(
            {name: param
             for name, param in self._model.named_buffers()})
        self._input_desc = input_desc
        self._device = device
        if output_desc is not None:
            # TODO: validate output shape/type against real outputs
            self._output_desc = output_desc
        else:
            self._output_desc = generate_output_desc(self._model,
                                                     self._input_desc,
                                                     self._device)
        self._model_desc = ModelDescription(self._input_desc,
                                            self._output_desc)
        if workdir:
            workdir = os.path.expandvars(os.path.expanduser(workdir))
            self._dir_ctx = None
            self._workdir = workdir
            os.makedirs(workdir, exist_ok=True)
        else:
            self._dir_ctx = tempfile.TemporaryDirectory(prefix="nnf_")
            self._workdir = self._dir_ctx.name

        self._const_folding = const_folding
        self._build_nnf = build_nnf
        # convert torch model to onnx
        if self._build_nnf:
            self._onnx_model_path = os.path.join(self._workdir, "nnf.onnx")
            convert_model_to_onnx(self._model, self._model_desc, self._device,
                                  self._onnx_model_path, self._const_folding)
        else:
            self._onnx_model_path = ""
        torch.cuda.empty_cache()

        # codegen
        self._codegen_flags = {"extern_result_memory": 1}
        self._codegen_flags.update(codegen_flags or {})
        if self._codegen_flags.get("training_mode",
                                   False) and self._const_folding:
            raise Exception("Const folding and training mode are incompatible")
        self._create_executor()

    def _create_executor(self):
        if "cuda" in self._device:
            rt_dir = os.path.join(self._workdir, "nnfusion_rt/cuda_codegen")
        elif "cpu" in self._device:
            raise Exception("CPU not supported yet")
        elif "rocm" in self._device:
            # TODO: support allocate torch tensors on ROCM device
            raise Exception("ROCm not supported yet")
        else:
            raise Exception("Unknown device {}".format(self._device))

        if self._build_nnf:
            flags_str = "-f {} ".format(self._model_format)
            flags_str += " ".join([
                "-f{}={}".format(k, v) for k, v in self._codegen_flags.items()
            ])
            codegen(self._onnx_model_path, flags_str, self._workdir)
            modify_nnfusion_rt(rt_dir)
            build(rt_dir)

        self._executor = Executor(rt_dir)

        nnf_inputs = self._executor.get_inputs()
        nnf_outputs = self._executor.get_outputs()
        real_inputs = {desc.name: desc for desc in self._input_desc}
        real_outputs = {desc.name: desc for desc in self._output_desc}
        if self._codegen_flags.get("training_mode", False):
            for name, tensor in self._torch_weights.items():
                assert name not in real_inputs, f"Duplicate inputs {name}"
                real_inputs[name] = tensor2desc(tensor, name=name)
        self._inputs = {}
        self._outputs = {}
        for desc in nnf_inputs:
            # Note: Not all inputs are consumed
            assert desc.name in real_inputs, f"nnf requires input {desc.name}, but it doesn\'t exist in session input desc"
            assert desc.shape == real_inputs[
                desc.
                name].shape, f"nnf requires input {desc.name} with shape {desc.shape}, but session input desc is {real_inputs[desc.name].shape}"
            assert desc.dtype == real_inputs[
                desc.
                name].dtype, f"nnf requires input {desc.name} with type {desc.dtype}, but session input desc is {real_inputs[desc.name].dtype}"
            if desc.name in self._torch_weights:
                self._inputs[desc.name] = cast_pytorch_tensor(
                    self._torch_weights[desc.name])
            else:
                self._inputs[desc.name] = None

        if bool(self._codegen_flags.get("extern_result_memory")) is not True:
            raise Exception("Please add extern_result_memory to codegen flags")

        for desc in nnf_outputs:
            assert self._codegen_flags.get(
                "training_mode", False
            ) or desc.name in real_outputs, f"nnf requires output {desc.name}, but it doesn\'t exist in session output desc"
            if desc.name in real_outputs:
                assert desc.shape == real_outputs[
                    desc.
                    name].shape, f"nnf requires output {desc.name} with shape {desc.shape}, but session output desc is {real_inputs[desc.name].shape}"
                assert desc.dtype == real_outputs[
                    desc.
                    name].dtype, f"nnf requires output {desc.name} with shape {desc.shape}, but session output desc is {real_inputs[desc.name].shape}"
            self._outputs[desc.name] = cast_pytorch_tensor(
                torch.zeros(desc.shape,
                            dtype=str2type[desc.dtype].torch_type,
                            device=self._device))

    def __call__(self, feed_data):
        return self.run_by_nnf(feed_data)

    def run_by_pytorch(self, feed_data):
        args = [feed_data[desc.name_] for desc in self._input_desc]
        with torch.no_grad():
            out = self._model(*args)
        return out

    def run_by_nnf(self, feed_data, check_nan=False):
        """
        Parameters:
            feed_data: a dict from name to PyTorch tensors, name should be presented in input desc.
            check_nan: check weight nan after forward
        
        Returns:
            a list of PyTorch tensors executed by NNFusion,
            they should be the same as origin PyTorch model forward results.
        """
        for name, tensor in feed_data.items():
            # TODO: check all inputs are presented in single forward
            if name in self._inputs:
                self._inputs[name] = cast_pytorch_tensor(tensor)
        self._executor(self._inputs, self._outputs)
        if check_nan and self.is_weights_nan():
            raise Exception("Nan found after execution")
        return [
            self._outputs[desc.name].reference for desc in self._output_desc
        ]

    def is_weights_nan(self):
        have_nan = False
        for name, weight in self._torch_weights.items():
            if bool(torch.isnan(weight).any()) or bool(
                    torch.isinf(weight).any()):
                logger.error("Nan or inf found in {}".format(name))
                # logger.error(weight)
                have_nan = True
        return have_nan


if __name__ == "__main__":
    pass