from __future__ import absolute_import, division, print_function, unicode_literals

import torch
import torch.nn as nn
import torch.nn.quantized as nnq
import torch.nn.quantized.dynamic as nnqd
from torch.quantization import prepare

from .default_mappings import (
    _EXCLUDE_QCONFIG_PROPAGATE_LIST,
    _INCLUDE_QCONFIG_PROPAGATE_LIST,
    DEFAULT_DYNAMIC_MODULE_MAPPING,
    DEFAULT_MODULE_MAPPING,
    DEFAULT_QAT_MODULE_MAPPING,
)


DEFAULT_NUMERIC_SUITE_COMPARE_MODEL_OUTPUT_WHITE_LIST = (
    set(DEFAULT_MODULE_MAPPING.values())
    | set(DEFAULT_QAT_MODULE_MAPPING.values())
    | set(DEFAULT_DYNAMIC_MODULE_MAPPING.values())
    | set(DEFAULT_MODULE_MAPPING.keys())
    | set(DEFAULT_QAT_MODULE_MAPPING.keys())
    | set(DEFAULT_DYNAMIC_MODULE_MAPPING.keys())
    | _INCLUDE_QCONFIG_PROPAGATE_LIST
) - _EXCLUDE_QCONFIG_PROPAGATE_LIST

NON_LEAF_MODULE_TO_ADD_OBSERVER_WHITE_LIST = {
    nnqd.Linear,
    nnq.Linear,
    nnqd.LSTM,
    nn.LSTM,
}


def _find_match(str_list, key_str, postfix):
    split_str = key_str.split(".")
    if split_str[-1] == postfix:
        match_string = "".join(key_str.split(".")[0:-1])
        for s2 in str_list:
            pattern1 = "".join(s2.split(".")[0:-1])
            pattern2 = "".join(s2.split(".")[0:-2])
            if match_string == pattern1:
                return s2
            if match_string == pattern2:
                return s2

        # For matching "fc.weight" and "fc._packed_params._packed_params"
        if postfix == "_packed_params":
            match_string = "".join(key_str.split(".")[0:-2])
            if len(match_string) == 0:
                return None
            for s2 in str_list:
                pattern1 = "".join(s2.split(".")[0:-1])
                pattern2 = "".join(s2.split(".")[0:-2])
                if match_string == pattern1:
                    return s2
                if match_string == pattern2:
                    return s2
    else:
        return None


def compare_weights(float_dict, quantized_dict):
    r"""Compare the weights of the float module with its corresponding quantized
    module. Return a dict with key corresponding to module names and each entry being
    a dictionary with two keys 'float' and 'quantized', containing the float and
    quantized weights. This dict can be used to compare and compute the quantization
    error of the weights of float and quantized models.

    Example usage:
        wt_compare_dict = compare_weights(float_model.state_dict(), qmodel.state_dict())
        for key in wt_compare_dict:
            print(key, compute_error(wt_compare_dict[key]['float'], wt_compare_dict[key]['quantized'].dequantize()))

    Args:
        float_dict: state dict of the float model
        quantized_dict: state dict of the quantized model

    Return:
        weight_dict: dict with key corresponding to module names and each entry being
        a dictionary with two keys 'float' and 'quantized', containing the float and
        quantized weights
    """
    weight_dict = {}
    for key in quantized_dict:
        match_key = _find_match(float_dict, key, "weight")
        if match_key is not None:
            weight_dict[key] = {}
            weight_dict[key]["float"] = float_dict[match_key]
            weight_dict[key]["quantized"] = quantized_dict[key]
            continue

        # For matching "fc.weight" and "fc._packed_params._packed_params"
        match_key = _find_match(float_dict, key, "_packed_params")
        if match_key is not None:
            weight_dict[key] = {}
            weight_dict[key]["float"] = float_dict[match_key]
            weight_dict[key]["quantized"] = quantized_dict[key][0]

        # For LSTM
        split_str = key.split(".")
        if split_str[-1] == "param" and split_str[-3] == "_all_weight_values":
            layer = split_str[-2]
            module_name = ".".join(split_str[:-3])
            float_weight_ih_key = module_name + ".weight_ih_l" + layer
            float_weight_hh_key = module_name + ".weight_hh_l" + layer
            if float_weight_ih_key in float_dict and float_weight_hh_key in float_dict:
                weight_dict[key] = {}
                weight_dict[key]["float"] = float_dict[float_weight_ih_key]
                weight_dict[key]["quantized"] = (
                    quantized_dict[key].__getstate__()[0][4][0].__getstate__()[0][0]
                )
                weight_dict[key]["float"] = float_dict[float_weight_hh_key]
                weight_dict[key]["quantized"] = (
                    quantized_dict[key].__getstate__()[0][4][1].__getstate__()[0][0]
                )

    return weight_dict


def _get_logger_dict_helper(mod, target_dict, prefix=""):
    r"""This is the helper function for get_logger_dict

    Args:
        mod: module we want to save all logger stats
        prefix: prefix for the current module
        target_dict: the dictionary used to save all logger stats
    """

    def get_prefix(prefix):
        return prefix if prefix == "" else prefix + "."

    for name, child in mod.named_children():
        if isinstance(child, Logger):
            target_dict[get_prefix(prefix) + "stats"] = child.stats
            break

    for name, child in mod.named_children():
        module_prefix = get_prefix(prefix) + name if prefix else name
        _get_logger_dict_helper(child, target_dict, module_prefix)


def get_logger_dict(mod, prefix=""):
    r"""Traverse the modules and save all logger stats into target dict.
    This is mainly used for quantization accuracy debug.

    Type of loggers supported:
        ShadowLogger: used to log the outputs of the quantized module and its
            matching float shadow module,
        OutputLogger: used to log the outputs of the modules

    Args:
        mod: module we want to save all logger stats
        prefix: prefix for the current module

    Return:
        target_dict: the dictionary used to save all logger stats
    """

    target_dict = {}
    _get_logger_dict_helper(mod, target_dict, prefix)
    return target_dict


class Logger(nn.Module):
    r"""Base class for stats logging
    """

    def __init__(self):
        super(Logger, self).__init__()
        self.stats = {}

    def forward(self, x):
        pass


class ShadowLogger(Logger):
    r"""Class used in Shadow module to record the outputs of the original and
    shadow modules.
    """

    def __init__(self):
        super(ShadowLogger, self).__init__()
        self.stats["float"] = None
        self.stats["quantized"] = None

    def forward(self, x, y):
        if len(x) > 1:
            x = x[0]
        if len(y) > 1:
            y = y[0]
        if self.stats["quantized"] is None:
            self.stats["quantized"] = x.detach()
        else:
            self.stats["quantized"] = torch.cat((self.stats["quantized"], x.detach()))

        if self.stats["float"] is None:
            self.stats["float"] = y.detach()
        else:
            self.stats["float"] = torch.cat((self.stats["float"], y.detach()))


class OutputLogger(Logger):
    r"""Class used to log the outputs of the module
    """

    def __init__(self):
        super(OutputLogger, self).__init__()
        self.stats["tensor_val"] = None

    def forward(self, x):
        if self.stats["tensor_val"] is None:
            self.stats["tensor_val"] = x
        else:
            self.stats["tensor_val"] = torch.cat((self.stats["tensor_val"], x))
        return x


def _convert_tuple_to_list(t):
    return list(_convert_tuple_to_list(x) for x in t) if type(t) is tuple else t


def _dequantize_tensor_list(t):
    return (
        list(_dequantize_tensor_list(x) for x in t)
        if type(t) is list
        else t.dequantize()
        if t.is_quantized
        else t
    )


class Shadow(nn.Module):
    r"""Shadow module attaches the float module to its matching quantized module
    as the shadow. Then it uses Logger module to process the outputs of both
    modules.

    Args:
        q_module: module quantized from float_module that we want to shadow
        float_module: float module used to shadow q_module
        Logger: type of logger used to process the outputs of q_module and
            float_module. ShadowLogger or custom loggers can be used.
    """

    def __init__(self, q_module, float_module, Logger):
        super(Shadow, self).__init__()
        self.orig_module = q_module
        self.shadow_module = float_module
        self.dequant = nnq.DeQuantize()
        self.logger = Logger()

    def forward(self, *x):
        xl = _convert_tuple_to_list(x)
        output = self.orig_module(*xl)
        xl_float = _dequantize_tensor_list(xl)
        shadow_output = self.shadow_module(*xl_float)
        self.logger(output, shadow_output)
        return output

    def add(self, x, y):
        output = self.orig_module.add(x, y)
        x = x.dequantize()
        y = y.dequantize()
        shadow_output = self.shadow_module.add(x, y)
        self.logger(output, shadow_output)
        return output

    def add_scalar(self, x, y):
        output = self.orig_module.add_scalar(x, y)
        x = x.dequantize()
        shadow_output = self.shadow_module.add_scalar(x, y)
        self.logger(output, shadow_output)
        return output

    def mul(self, x, y):
        output = self.orig_module.mul(x, y)
        x = x.dequantize()
        y = y.dequantize()
        shadow_output = self.shadow_module.mul(x, y)
        self.logger(output, shadow_output)
        return output

    def mul_scalar(self, x, y):
        output = self.orig_module.mul_scalar(x, y)
        x = x.dequantize()
        shadow_output = self.shadow_module.mul_scalar(x, y)
        self.logger(output, shadow_output)
        return output

    def cat(self, x, dim=0):
        output = self.orig_module.cat(x, dim)
        x = [y.dequantize() for y in x]
        shadow_output = self.shadow_module.cat(x, dim)
        self.logger(output, shadow_output)
        return output

    def add_relu(self, x, y):
        output = self.orig_module.add_relu(x, y)
        x = x.dequantize()
        y = y.dequantize()
        shadow_output = self.shadow_module.add_relu(x, y)
        self.logger(output, shadow_output)
        return output


def prepare_model_with_stubs(float_module, q_module, module_swap_list, Logger):
    r"""Prepare the model by attaching the float module to its matching quantized
    module as the shadow if the float module type is in module_swap_list.

    Example usage:
        prepare_model_with_stubs(float_model, q_model, module_swap_list, Logger)
        q_model(data)
        ob_dict = get_logger_dict(q_model)

    Args:
        float_module: float module used to generate the q_module
        q_module: module quantized from float_module
        module_swap_list: list of float module types to attach the shadow
        Logger: type of logger to be used in shadow module to process the outputs of
            quantized module and its float shadow module
    """

    float_module_children = {}
    for name, mod in float_module.named_children():
        float_module_children[name] = mod

    reassign = {}
    for name, mod in q_module.named_children():
        if name not in float_module_children:
            continue

        float_mod = float_module_children[name]

        if type(float_mod) not in module_swap_list:
            prepare_model_with_stubs(float_mod, mod, module_swap_list, Logger)

        if type(float_mod) in module_swap_list:
            reassign[name] = Shadow(mod, float_mod, Logger)

    for key, value in reassign.items():
        q_module._modules[key] = value


def compare_model_stub(
    float_model, q_model, module_swap_list, *data, Logger=ShadowLogger
):
    r"""Compare quantized module in a model with its floating point counterpart,
    feeding both of them the same input. Return a dict with key corresponding to
    module names and each entry being a dictionary with two keys 'float' and
    'quantized', containing the output tensors of quantized and its matching
    float shadow module. This dict can be used to compare and compute the module
    level quantization error.

    This function first call prepare_model_with_stubs() to swap the quantized
    module that we want to compare with the Shadow module, which takes quantized
    module, corresponding float module and logger as input, and creates a forward
    path inside to make the float module to shadow quantized module sharing the
    same input. The logger can be customizable, default logger is ShadowLogger
    and it will save the outputs of the quantized module and float module that
    can be used to compute the module level quantization error.

    Example usage:
        module_swap_list = [torchvision.models.quantization.resnet.QuantizableBasicBlock]
        ob_dict = compare_model_stub(float_model,qmodel,module_swap_list, data)
        for key in ob_dict:
            print(key, compute_error(ob_dict[key]['float'], ob_dict[key]['quantized'].dequantize()))

    Args:
        float_model: float model used to generate the q_model
        q_model: model quantized from float_model
        module_swap_list: list of float module types at which shadow modules will
            be attached.
        data: input data used to run the prepared q_model
        Logger: type of logger to be used in shadow module to process the outputs of
            quantized module and its float shadow module
    """
    prepare_model_with_stubs(float_model, q_model, module_swap_list, Logger)
    q_model(*data)
    ob_dict = get_logger_dict(q_model)
    return ob_dict


def get_matching_activations(float_module, q_module):
    r"""Find the matching activation between float and quantized modules.

    Args:
        float_module: float module used to generate the q_module
        q_module: module quantized from float_module

    Return:
        act_dict: dict with key corresponding to quantized module names and each
        entry being a dictionary with two keys 'float' and 'quantized', containing
        the matching float and quantized activations
    """
    float_dict = get_logger_dict(float_module)
    quantized_dict = get_logger_dict(q_module)
    act_dict = {}
    for key in quantized_dict:
        match_key = _find_match(sorted(float_dict, reverse=True), key, "stats")
        if match_key is not None:
            act_dict[key] = {}
            act_dict[key]["float"] = float_dict[match_key]["tensor_val"]
            act_dict[key]["quantized"] = quantized_dict[key]["tensor_val"]
    return act_dict


def prepare_model_outputs(
    float_module,
    q_module,
    Logger=OutputLogger,
    white_list=DEFAULT_NUMERIC_SUITE_COMPARE_MODEL_OUTPUT_WHITE_LIST,
):
    r"""Prepare the model by attaching the logger to both float module
    and quantized module if they are in the white_list.

    Args:
        float_module: float module used to generate the q_module
        q_module: module quantized from float_module
        Logger: type of logger to be attached to float_module and q_module
        white_list: list of module types to attach logger
    """
    qconfig_debug = torch.quantization.QConfig(activation=Logger, weight=None)
    float_module.qconfig = qconfig_debug
    prepare(float_module, inplace=True, white_list=white_list)
    q_module.qconfig = qconfig_debug
    prepare(
        q_module,
        inplace=True,
        white_list=white_list,
        observer_non_leaf_module_list=NON_LEAF_MODULE_TO_ADD_OBSERVER_WHITE_LIST,
    )


def compare_model_outputs(
    float_model,
    q_model,
    *data,
    Logger=OutputLogger,
    white_list=DEFAULT_NUMERIC_SUITE_COMPARE_MODEL_OUTPUT_WHITE_LIST,
):
    r"""Compare output activations between float and quantized models at
    corresponding locations for the same input. Return a dict with key corresponding
    to quantized module names and each entry being a dictionary with two keys
    'float' and 'quantized', containing the activations of quantized model and
    float model at matching locations. This dict can be used to compare and
    compute the propagation quantization error.

    Example usage:
        act_compare_dict = compare_model_outputs(float_model, qmodel, data)
        for key in act_compare_dict:
            print(key, compute_error(act_compare_dict[key]['float'], act_compare_dict[key]['quantized'].dequantize()))

    Args:
        float_model: float model used to generate the q_model
        q_model: model quantized from float_model
        data: input data used to run the prepared float_model and q_model
        Logger: type of logger to be attached to float_module and q_module
        white_list: list of module types to attach logger

    Return:
        act_compare_dict: dict with key corresponding to quantized module names
        and each entry being a dictionary with two keys 'float' and 'quantized',
        containing the matching float and quantized activations
    """
    prepare_model_outputs(float_model, q_model, Logger, white_list)
    float_model(*data)
    q_model(*data)
    act_compare_dict = get_matching_activations(float_model, q_model)
    return act_compare_dict
