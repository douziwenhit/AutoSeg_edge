import torch
import torch.nn as nn
import torch.nn.functional as F
from operator_pool import *
from torch.autograd import Variable
from genotypes import PRIMITIVES
# from utils.darts_utils import drop_path, compute_speed, compute_speed_tensorrt
from pdb import set_trace as bp
from seg_oprs import Head
import numpy as np


# https://github.com/YongfeiYan/Gumbel_Softmax_VAE/blob/master/gumbel_softmax_vae.py
def sample_gumbel(shape, eps=1e-20):
    U = torch.rand(shape)
    U = U.cuda()
    return -torch.log(-torch.log(U + eps) + eps)


def gumbel_softmax_sample(logits, temperature=1):
    y = logits + sample_gumbel(logits.size())
    return F.softmax(y / temperature, dim=-1)


def gumbel_softmax(logits, temperature=1, hard=False):
    """
    ST-gumple-softmax
    input: [*, n_class]
    return: flatten --> [*, n_class] an one-hot vector
    """
    y = gumbel_softmax_sample(logits, temperature)

    if not hard:
        return y

    shape = y.size()
    _, ind = y.max(dim=-1)
    y_hard = torch.zeros_like(y).view(-1, shape[-1])
    y_hard.scatter_(1, ind.view(-1, 1), 1)
    y_hard = y_hard.view(*shape)
    # Set gradients w.r.t. y_hard gradients w.r.t. y
    y_hard = (y_hard - y).detach() + y
    return y_hard


class MixedOp(nn.Module):

    def __init__(self, C_in, C_out, stride=1, width_mult_list=[1.]):
        super(MixedOp, self).__init__()
        self._ops = nn.ModuleList()
        self._width_mult_list = width_mult_list
        for primitive in PRIMITIVES:
            op = OPS[primitive](C_in, C_out, stride, True, width_mult_list=width_mult_list)
            self._ops.append(op)

    def set_prun_ratio(self, ratio):
        for op in self._ops:
            op.set_ratio(ratio)

    def forward(self, x, weights, thetas):
        # int: force #channel; tensor: arch_ratio; float(<=1): force width
        x.cuda()
        weights.cuda()
        result = 0
        if isinstance(thetas[0], torch.Tensor):
            ratio0 = self._width_mult_list[thetas[0].argmax()]
            r_score0 = thetas[0][thetas[0].argmax()]
        else:
            ratio0 = thetas[0]
            r_score0 = 1.
        if isinstance(thetas[1], torch.Tensor):
            ratio1 = self._width_mult_list[thetas[1].argmax()]
            r_score1 = thetas[1][thetas[1].argmax()]
        else:
            ratio1 = thetas[1]
            r_score1 = 1.
        self.set_prun_ratio((ratio0, ratio1))
        for w, op in zip(weights, self._ops):
            op(x).cuda()
            result = result + op(x) * w * r_score0 * r_score1 #  每一次的结果相加
        return result

    def forward_latency(self, size, weights, thetas):
        # int: force #channel; tensor: arch_ratio; float(<=1): force width
        result = 0
        if isinstance(thetas[0], torch.Tensor):
            ratio0 = self._width_mult_list[thetas[0].argmax()]
            r_score0 = thetas[0][thetas[0].argmax()]
        else:
            ratio0 = thetas[0]
            r_score0 = 1.
        if isinstance(thetas[1], torch.Tensor):
            ratio1 = self._width_mult_list[thetas[1].argmax()]
            r_score1 = thetas[1][thetas[1].argmax()]
        else:
            ratio1 = thetas[1]
            r_score1 = 1.
        self.set_prun_ratio((ratio0, ratio1))
        for w, op in zip(weights, self._ops):
            latency, size_out = op.forward_latency(size)
            result = result + latency * w * r_score0 * r_score1
        return result, size_out

    def forward_flops(self, size, fai, ratio):
        # int: force #channel; tensor: arch_ratio; float(<=1): force width
        result = 0
        if isinstance(ratio[0], torch.Tensor):
            ratio0 = self._width_mult_list[ratio[0].argmax()]
            r_score0 = ratio[0][ratio[0].argmax()]
        else:
            ratio0 = ratio[0]
            r_score0 = 1.
        if isinstance(ratio[1], torch.Tensor):
            ratio1 = self._width_mult_list[ratio[1].argmax()]
            r_score1 = ratio[1][ratio[1].argmax()]
        else:
            ratio1 = ratio[1]
            r_score1 = 1.


        self.set_prun_ratio((ratio0, ratio1))

        for w, op in zip(fai, self._ops):
            flops, size_out = op.forward_flops(size)
            result = result + flops * w * r_score0 * r_score1
        return result, size_out

class Cell(nn.Module):
    def __init__(self, C_in, C_out=None, down=True, width_mult_list=[1.]):
        super(Cell, self).__init__()
        self._C_in = C_in
        if C_out is None: C_out = C_in
        self._C_out = C_out
        self._down = down
        self._width_mult_list = width_mult_list

        self._op = MixedOp(C_in, C_out, width_mult_list=width_mult_list)

        if self._down:
            self.downsample = MixedOp(C_in, C_in*2, stride=2, width_mult_list=width_mult_list)

    def forward(self, input, fais, thetas):
        # thetas: (in, out, down)
        out = self._op(input, fais, (thetas[0], thetas[1]))
        assert (self._down and (thetas[2] is not None)) or ((not self._down) and (thetas[2] is None))
        down = self.downsample(input, fais, (thetas[0], thetas[2])) if self._down else None
        return out, down

    def forward_latency(self, size, fais, thetas):
        # thetas: (in, out, down)
        out = self._op.forward_latency(size, fais, (thetas[0], thetas[1]))
        assert (self._down and (thetas[2] is not None)) or ((not self._down) and (thetas[2] is None))
        down = self.downsample.forward_latency(size, fais, (thetas[0], thetas[2])) if self._down else None
        return out, down

    def forward_flops(self, size, fais, thetas):
        # thetas: (in, out, down)
        out = self._op.forward_flops(size, fais, (thetas[0], thetas[1]))
        assert (self._down and (thetas[2] is not None)) or ((not self._down) and (thetas[2] is None))
        down = self.downsample.forward_latency(size, fais, (thetas[0], thetas[2])) if self._down else None
        return out, down

class Network_Multi_Path(nn.Module):
    def __init__(self, num_classes=19, layers=16, criterion=nn.CrossEntropyLoss(ignore_index=-1), Fch=12, width_mult_list=[1.,], prun_modes=['arch_ratio',], stem_head_width=[(1., 1.),]):
        super(Network_Multi_Path, self).__init__()
        self._num_classes = num_classes
        assert layers >= 3
        self._layers = layers
        self._criterion = criterion
        self._Fch = Fch
        self._width_mult_list = width_mult_list
        self._prun_modes = prun_modes
        self.prun_mode = None # prun_mode is higher priority than _prun_modes
        self._stem_head_width = stem_head_width
        self._flops = 0
        self._params = 0
        """
            stem由5个3*3的卷积组成
            """
        self.stem = nn.ModuleList([
            nn.Sequential(
                ConvNorm(3, self.num_filters(2, stem_ratio)*2, kernel_size=3, stride=2, padding=1, bias=False, groups=1, slimmable=False),
                BasicResidual2x(self.num_filters(2, stem_ratio)*2, self.num_filters(4, stem_ratio)*2, kernel_size=3, stride=2, groups=1, slimmable=False),
                BasicResidual2x(self.num_filters(4, stem_ratio)*2, self.num_filters(8, stem_ratio), kernel_size=3, stride=2, groups=1, slimmable=False)
            ) for stem_ratio, _ in self._stem_head_width ])
        #构建基础Cell
        self.cells = nn.ModuleList()
        for l in range(layers):# 网络层数
            cells = nn.ModuleList()
            if l == 0:
                # first node has only one input (prev cell's output)
                cells.append(Cell(self.num_filters(8), width_mult_list=width_mult_list))
            elif l == 1:#第二层
                cells.append(Cell(self.num_filters(8), width_mult_list=width_mult_list))
                cells.append(Cell(self.num_filters(16), width_mult_list=width_mult_list))
            elif l < layers - 1:#中间层
                cells.append(Cell(self.num_filters(8), width_mult_list=width_mult_list))
                cells.append(Cell(self.num_filters(16), width_mult_list=width_mult_list))
                cells.append(Cell(self.num_filters(32), down=False, width_mult_list=width_mult_list))
            else:#最后一层
                cells.append(Cell(self.num_filters(8), down=False, width_mult_list=width_mult_list))
                cells.append(Cell(self.num_filters(16), down=False, width_mult_list=width_mult_list))
                cells.append(Cell(self.num_filters(32), down=False, width_mult_list=width_mult_list))
            self.cells.append(cells)

        self.refine32 = nn.ModuleList([
            nn.ModuleList([
                ConvNorm(self.num_filters(32, head_ratio), self.num_filters(16, head_ratio), kernel_size=1, bias=False, groups=1, slimmable=False),
                ConvNorm(self.num_filters(32, head_ratio), self.num_filters(16, head_ratio), kernel_size=3, padding=1, bias=False, groups=1, slimmable=False),
                ConvNorm(self.num_filters(16, head_ratio), self.num_filters(8, head_ratio), kernel_size=1, bias=False, groups=1, slimmable=False),
                ConvNorm(self.num_filters(16, head_ratio), self.num_filters(8, head_ratio), kernel_size=3, padding=1, bias=False, groups=1, slimmable=False)]) for _, head_ratio in self._stem_head_width ])
        self.refine16 = nn.ModuleList([
            nn.ModuleList([
                ConvNorm(self.num_filters(16, head_ratio), self.num_filters(8, head_ratio), kernel_size=1, bias=False, groups=1, slimmable=False),
                ConvNorm(self.num_filters(16, head_ratio), self.num_filters(8, head_ratio), kernel_size=3, padding=1, bias=False, groups=1, slimmable=False)]) for _, head_ratio in self._stem_head_width ])

        self.head0 = nn.ModuleList([ Head(self.num_filters(8, head_ratio), num_classes, False) for _, head_ratio in self._stem_head_width ])
        self.head1 = nn.ModuleList([ Head(self.num_filters(8, head_ratio), num_classes, False) for _, head_ratio in self._stem_head_width ])
        self.head2 = nn.ModuleList([ Head(self.num_filters(8, head_ratio), num_classes, False) for _, head_ratio in self._stem_head_width ])
        self.head02 = nn.ModuleList([ Head(self.num_filters(8, head_ratio)*2, num_classes, False) for _, head_ratio in self._stem_head_width ])
        self.head12 = nn.ModuleList([ Head(self.num_filters(8, head_ratio)*2, num_classes, False) for _, head_ratio in self._stem_head_width ])

        # contains arch_param names: {"fais": fais, "mjus": mjus, "thetas": thetas}
        self._arch_names = []
        self._arch_parameters = []
        for i in range(len(self._prun_modes)):
            arch_name, arch_param = self._build_arch_parameters(i)
            self._arch_names.append(arch_name)
            self. _arch_parameters.append(arch_param)
            self._reset_arch_parameters(i)
        # switch set of arch if we have more than 1 arch
        self.arch_idx = 0

    def num_filters(self, scale, width=1.0):
        return int(np.round(scale * self._Fch * width))

    def new(self):
        model_new = Network(self._num_classes, self._layers, self._criterion, self._Fch).cuda()
        for x, y in zip(model_new.arch_parameters(), self.arch_parameters()):
                x.data.copy_(y.data)
        return model_new

    def sample_prun_ratio(self, mode="arch_ratio"):
        '''
        mode: "min"|"max"|"random"|"arch_ratio"(default)
        '''
        assert mode in ["min", "max", "random", "arch_ratio"]
        if mode == "arch_ratio":
            thetas = self._arch_names[0]["thetas"]
            thetas0 = getattr(self, thetas[0])
            thetas0_sampled = []
            for layer in range(self._layers - 1):
                thetas0_sampled.append(gumbel_softmax(F.log_softmax(thetas0[layer], dim=-1), hard=True))
            thetas1 = getattr(self, thetas[1])
            thetas1_sampled = []
            for layer in range(self._layers - 1):
                thetas1_sampled.append(gumbel_softmax(F.log_softmax(thetas1[layer], dim=-1), hard=True))
            thetas2 = getattr(self, thetas[2])
            thetas2_sampled = []
            for layer in range(self._layers - 2):
                thetas2_sampled.append(gumbel_softmax(F.log_softmax(thetas2[layer], dim=-1), hard=True))
            return [thetas0_sampled, thetas1_sampled, thetas2_sampled]
        elif mode == "min":
            thetas0_sampled = []
            for layer in range(self._layers - 1):
                thetas0_sampled.append(self._width_mult_list[0])
            thetas1_sampled = []
            for layer in range(self._layers - 1):
                thetas1_sampled.append(self._width_mult_list[0])
            thetas2_sampled = []
            for layer in range(self._layers - 2):
                thetas2_sampled.append(self._width_mult_list[0])
            return [thetas0_sampled, thetas1_sampled, thetas2_sampled]
        elif mode == "max":
            thetas0_sampled = []
            for layer in range(self._layers - 1):
                thetas0_sampled.append(self._width_mult_list[-1])
            thetas1_sampled = []
            for layer in range(self._layers - 1):
                thetas1_sampled.append(self._width_mult_list[-1])
            thetas2_sampled = []
            for layer in range(self._layers - 2):
                thetas2_sampled.append(self._width_mult_list[-1])
            return [thetas0_sampled, thetas1_sampled, thetas2_sampled]
        elif mode == "random":
            thetas0_sampled = []
            for layer in range(self._layers - 1):
                thetas0_sampled.append(np.random.choice(self._width_mult_list))
            thetas1_sampled = []
            for layer in range(self._layers - 1):
                thetas1_sampled.append(np.random.choice(self._width_mult_list))
            thetas2_sampled = []
            for layer in range(self._layers - 2):
                thetas2_sampled.append(np.random.choice(self._width_mult_list))
            return [thetas0_sampled, thetas1_sampled, thetas2_sampled]

    def forward(self, input):
        # out_prev: cell-state
        # index 0: keep; index 1: down
        stem = self.stem[0]
        refine16 = self.refine16[0]
        refine32 = self.refine32[0]
        head0 = self.head0[0]
        head1 = self.head1[0]
        head2 = self.head2[0]
        head02 = self.head02[0]
        head12 = self.head12[0]

        fais0 = F.softmax(getattr(self, self._arch_names[0]["fais"][0]), dim=-1).cuda()
        fais1 = F.softmax(getattr(self, self._arch_names[0]["fais"][1]), dim=-1).cuda()
        fais2 = F.softmax(getattr(self, self._arch_names[0]["fais"][2]), dim=-1).cuda()
        fais = [fais0, fais1, fais2]
        mjus1 = F.softmax(getattr(self, self._arch_names[0]["mjus"][0]), dim=-1).cuda()
        mjus2 = F.softmax(getattr(self, self._arch_names[0]["mjus"][1]), dim=-1).cuda()
        mjus = [None, mjus1, mjus2]
        if self.prun_mode is not None:
            thetas = self.sample_prun_ratio(mode=self.prun_mode)
        else:
            thetas = self.sample_prun_ratio(mode=self._prun_modes[0])

        out_prev = [[stem(input), None]] # stem: one cell
        # i: layer | j: scale
        for i, cells in enumerate(self.cells):
            # layers
            out = []
            for j, cell in enumerate(cells):
                # scales
                # out,down -- 0: from down; 1: from keep
                out0 = None; out1 = None
                down0 = None; down1 = None
                fai = fais[j][i-j]
                # ratio: (in, out, down)
                # int: force #channel; tensor: arch_ratio; float(<=1): force width
                if i == 0 and j == 0:
                    # first cell
                    ratio = (self._stem_head_width[0][0], thetas[j][i-j], thetas[j+1][i-j])
                elif i == self._layers - 1:
                    # cell in last layer
                    if j == 0:
                        ratio = (thetas[j][i-j-1], self._stem_head_width[0][1], None)
                    else:
                        ratio = (thetas[j][i-j], self._stem_head_width[0][1], None)
                elif j == 2:
                    # cell in last scale: no down ratio "None"
                    ratio = (thetas[j][i-j], thetas[j][i-j+1], None)
                else:
                    if j == 0:
                        ratio = (thetas[j][i-j-1], thetas[j][i-j], thetas[j+1][i-j])
                    else:
                        ratio = (thetas[j][i-j], thetas[j][i-j+1], thetas[j+1][i-j])
                # out,down -- 0: from down; 1: from keep
                if j == 0:
                    out1, down1 = cell(out_prev[0][0], fai, ratio)
                    out.append((out1, down1))
                else:
                    if i == j:
                        out0, down0 = cell(out_prev[j-1][1], fai, ratio)
                        out.append((out0, down0))
                    else:
                        if mjus[j][i-j-1][0] > 0:
                            out0, down0 = cell(out_prev[j-1][1], fai, ratio)
                        if mjus[j][i-j-1][1] > 0:
                            out1, down1 = cell(out_prev[j][0], fai, ratio)
                        out.append((
                            sum(w * out for w, out in zip(mjus[j][i-j-1], [out0, out1])),
                            sum(w * down if down is not None else 0 for w, down in zip(mjus[j][i-j-1], [down0, down1])),
                            ))
            out_prev = out
        ###################################
        out0 = None; out1 = None; out2 = None

        out0 = out[0][0]
        out1 = F.interpolate(refine16[0](out[1][0]), scale_factor=2, mode='bilinear', align_corners=True)
        out1 = refine16[1](torch.cat([out1, out[0][0]], dim=1))
        out2 = F.interpolate(refine32[0](out[2][0]), scale_factor=2, mode='bilinear', align_corners=True)
        out2 = refine32[1](torch.cat([out2, out[1][0]], dim=1))
        out2 = F.interpolate(refine32[2](out2), scale_factor=2, mode='bilinear', align_corners=True)
        out2 = refine32[3](torch.cat([out2, out[0][0]], dim=1))

        pred0 = head0(out0)
        pred1 = head1(out1)
        pred2 = head2(out2)
        pred02 = head02(torch.cat([out0, out2], dim=1))
        pred12 = head12(torch.cat([out1, out2], dim=1))

        if not self.training:
            pred0 = F.interpolate(pred0, scale_factor=8, mode='bilinear', align_corners=True)
            pred1 = F.interpolate(pred1, scale_factor=8, mode='bilinear', align_corners=True)
            pred2 = F.interpolate(pred2, scale_factor=8, mode='bilinear', align_corners=True)
            pred02 = F.interpolate(pred02, scale_factor=8, mode='bilinear', align_corners=True)
            pred12 = F.interpolate(pred12, scale_factor=8, mode='bilinear', align_corners=True)
        return pred0, pred1, pred2, pred02, pred12
        ###################################

    def forward_latency(self, size, fai=True, beta=True, ratio=True):
        # out_prev: cell-state
        # index 0: keep; index 1: down
        stem = self.stem[0]

        if fai:
            fais0 = F.softmax(getattr(self, self._arch_names[0]["fais"][0]), dim=-1)
            fais1 = F.softmax(getattr(self, self._arch_names[0]["fais"][1]), dim=-1)
            fais2 = F.softmax(getattr(self, self._arch_names[0]["fais"][2]), dim=-1)
            fais = [fais0, fais1, fais2]
        else:
            fais = [
                torch.ones_like(getattr(self, self._arch_names[0]["fais"][0])).cuda() * 1./len(PRIMITIVES),
                torch.ones_like(getattr(self, self._arch_names[0]["fais"][1])).cuda() * 1./len(PRIMITIVES),
                torch.ones_like(getattr(self, self._arch_names[0]["fais"][2])).cuda() * 1./len(PRIMITIVES)]
        if beta:
            mjus1 = F.softmax(getattr(self, self._arch_names[0]["mjus"][0]), dim=-1)
            mjus2 = F.softmax(getattr(self, self._arch_names[0]["mjus"][1]), dim=-1)
            mjus = [None, mjus1, mjus2]
        else:
            mjus = [
                None,
                torch.ones_like(getattr(self, self._arch_names[0]["mjus"][0])).cuda() * 1./2,
                torch.ones_like(getattr(self, self._arch_names[0]["mjus"][1])).cuda() * 1./2]
        if ratio:
            # thetas = self.sample_prun_ratio(mode='arch_ratio')
            if self.prun_mode is not None:
                thetas = self.sample_prun_ratio(mode=self.prun_mode)
            else:
                thetas = self.sample_prun_ratio(mode=self._prun_modes[0])
        else:
            thetas = self.sample_prun_ratio(mode='max')

        stem_latency = 0
        latency, size = stem[0].forward_latency(size); stem_latency = stem_latency + latency
        latency, size = stem[1].forward_latency(size); stem_latency = stem_latency + latency
        latency, size = stem[2].forward_latency(size); stem_latency = stem_latency + latency
        out_prev = [[size, None]] # stem: one cell
        latency_total = [[stem_latency, 0], [0, 0], [0, 0]] # (out, down)

        # i: layer | j: scale
        for i, cells in enumerate(self.cells):
            # layers
            out = []
            latency = []
            for j, cell in enumerate(cells):
                # scales
                # out,down -- 0: from down; 1: from keep
                out0 = None; out1 = None
                down0 = None; down1 = None
                fai = fais[j][i-j]
                # ratio: (in, out, down)
                # int: force #channel; tensor: arch_ratio; float(<=1): force width
                if i == 0 and j == 0:
                    # first cell
                    ratio = (self._stem_head_width[0][0], thetas[j][i-j], thetas[j+1][i-j])
                elif i == self._layers - 1:
                    # cell in last layer
                    if j == 0:
                        ratio = (thetas[j][i-j-1], self._stem_head_width[0][1], None)
                    else:
                        ratio = (thetas[j][i-j], self._stem_head_width[0][1], None)
                elif j == 2:
                    # cell in last scale
                    ratio = (thetas[j][i-j], thetas[j][i-j+1], None)
                else:
                    if j == 0:
                        ratio = (thetas[j][i-j-1], thetas[j][i-j], thetas[j+1][i-j])
                    else:
                        ratio = (thetas[j][i-j], thetas[j][i-j+1], thetas[j+1][i-j])
                # out,down -- 0: from down; 1: from keep
                if j == 0:
                    out1, down1 = cell.forward_latency(out_prev[0][0], fai, ratio)
                    out.append((out1[1], down1[1] if down1 is not None else None))
                    latency.append([out1[0], down1[0] if down1 is not None else None])
                else:
                    if i == j:
                        out0, down0 = cell.forward_latency(out_prev[j-1][1], fai, ratio)
                        out.append((out0[1], down0[1] if down0 is not None else None))
                        latency.append([out0[0], down0[0] if down0 is not None else None])
                    else:
                        if mjus[j][i-j-1][0] > 0:
                            # from down
                            out0, down0 = cell.forward_latency(out_prev[j-1][1], fai, ratio)
                        if mjus[j][i-j-1][1] > 0:
                            # from keep
                            out1, down1 = cell.forward_latency(out_prev[j][0], fai, ratio)
                        assert (out0 is None and out1 is None) or out0[1] == out1[1]
                        assert (down0 is None and down1 is None) or down0[1] == down1[1]
                        out.append((out0[1], down0[1] if down0 is not None else None))
                        latency.append([
                            sum(w * out for w, out in zip(mjus[j][i-j-1], [out0[0], out1[0]])),
                            sum(w * down if down is not None else 0 for w, down in zip(mjus[j][i-j-1], [down0[0] if down0 is not None else None, down1[0] if down1 is not None else None])),
                        ])
            out_prev = out
            for ii, lat in enumerate(latency):
                # layer: i | scale: ii
                if ii == 0:
                    # only from keep
                    if lat[0] is not None: latency_total[ii][0] = latency_total[ii][0] + lat[0]
                    if lat[1] is not None: latency_total[ii][1] = latency_total[ii][0] + lat[1]
                else:
                    if i == ii:
                        # only from down
                        if lat[0] is not None: latency_total[ii][0] = latency_total[ii-1][1] + lat[0]
                        if lat[1] is not None: latency_total[ii][1] = latency_total[ii-1][1] + lat[1]
                    else:
                        if lat[0] is not None: latency_total[ii][0] = mjus[j][i-j-1][1] * latency_total[ii][0] + mjus[j][i-j-1][0] * latency_total[ii-1][1] + lat[0]
                        if lat[1] is not None: latency_total[ii][1] = mjus[j][i-j-1][1] * latency_total[ii][0] + mjus[j][i-j-1][0] * latency_total[ii-1][1] + lat[1]
        ###################################
        latency0 = latency_total[0][0]
        latency1 = latency_total[1][0]
        latency2 = latency_total[2][0]
        latency = sum([latency0, latency1, latency2])
        return latency
        ###################################

    def forward_flops(self, size, fai=True, beta=True, ratio=True):
        # out_prev: cell-state
        # index 0: keep; index 1: down
        stem = self.stem[0]

        if fai:
            fais0 = F.softmax(getattr(self, self._arch_names[0]["fais"][0]), dim=-1)
            fais1 = F.softmax(getattr(self, self._arch_names[0]["fais"][1]), dim=-1)
            fais2 = F.softmax(getattr(self, self._arch_names[0]["fais"][2]), dim=-1)
            fais = [fais0, fais1, fais2]
        else:
            fais = [
                torch.ones_like(getattr(self, self._arch_names[0]["fais"][0])).cuda() * 1./len(PRIMITIVES),
                torch.ones_like(getattr(self, self._arch_names[0]["fais"][1])).cuda() * 1./len(PRIMITIVES),
                torch.ones_like(getattr(self, self._arch_names[0]["fais"][2])).cuda() * 1./len(PRIMITIVES)]
        if beta:
            mjus1 = F.softmax(getattr(self, self._arch_names[0]["mjus"][0]), dim=-1)
            mjus2 = F.softmax(getattr(self, self._arch_names[0]["mjus"][1]), dim=-1)
            mjus = [None, mjus1, mjus2]
        else:
            mjus = [
                None,
                torch.ones_like(getattr(self, self._arch_names[0]["mjus"][0])).cuda() * 1./2,
                torch.ones_like(getattr(self, self._arch_names[0]["mjus"][1])).cuda() * 1./2]
        if ratio:
            # thetas = self.sample_prun_ratio(mode='arch_ratio')
            if self.prun_mode is not None:
                thetas = self.sample_prun_ratio(mode=self.prun_mode)
            else:
                thetas = self.sample_prun_ratio(mode=self._prun_modes[0])
        else:
            thetas = self.sample_prun_ratio(mode='max')

        stem_flops = 0
        flops, size = stem[0].forward_flops(size); stem_flops = stem_flops + flops
        flops, size = stem[1].forward_flops(size); stem_flops = stem_flops + flops
        flops, size = stem[2].forward_flops(size); stem_flops = stem_flops + flops
        out_prev = [[size, None]] # stem: one cell
        flops_total = [[stem_flops, 0], [0, 0], [0, 0]] # (out, down)

        # i: layer | j: scale
        for i, cells in enumerate(self.cells):
            # layers
            out = []
            flops = []
            for j, cell in enumerate(cells):
                # scales
                # out,down -- 0: from down; 1: from keep
                out0 = None; out1 = None
                down0 = None; down1 = None
                fai = fais[j][i-j]
                # ratio: (in, out, down)
                # int: force #channel; tensor: arch_ratio; float(<=1): force width
                if i == 0 and j == 0:
                    # first cell
                    ratio = (self._stem_head_width[0][0], thetas[j][i-j], thetas[j+1][i-j])
                elif i == self._layers - 1:
                    # cell in last layer
                    if j == 0:
                        ratio = (thetas[j][i-j-1], self._stem_head_width[0][1], None)
                    else:
                        ratio = (thetas[j][i-j], self._stem_head_width[0][1], None)
                elif j == 2:
                    # cell in last scale
                    ratio = (thetas[j][i-j], thetas[j][i-j+1], None)
                else:
                    if j == 0:
                        ratio = (thetas[j][i-j-1], thetas[j][i-j], thetas[j+1][i-j])
                    else:
                        ratio = (thetas[j][i-j], thetas[j][i-j+1], thetas[j+1][i-j])
                # out,down -- 0: from down; 1: from keep
                if j == 0:
                    out1, down1 = cell.forward_flops(out_prev[0][0], fai, ratio)
                    out.append((out1[1], down1[1] if down1 is not None else None))
                    flops.append([out1[0], down1[0] if down1 is not None else None])
                else:
                    if i == j:
                        out0, down0 = cell.forward_flops(out_prev[j-1][1], fai, ratio)
                        out.append((out0[1], down0[1] if down0 is not None else None))
                        flops.append([out0[0], down0[0] if down0 is not None else None])
                    else:
                        if mjus[j][i-j-1][0] > 0:
                            # from down
                            out0, down0 = cell.forward_flops(out_prev[j-1][1], fai, ratio)
                        if mjus[j][i-j-1][1] > 0:
                            # from keep
                            out1, down1 = cell.forward_flops(out_prev[j][0], fai, ratio)
                        assert (out0 is None and out1 is None) or out0[1] == out1[1]
                        assert (down0 is None and down1 is None) or down0[1] == down1[1]
                        out.append((out0[1], down0[1] if down0 is not None else None))
                        flops.append([
                            sum(w * out for w, out in zip(mjus[j][i-j-1], [out0[0], out1[0]])),
                            sum(w * down if down is not None else 0 for w, down in zip(mjus[j][i-j-1], [down0[0] if down0 is not None else None, down1[0] if down1 is not None else None])),
                        ])
            out_prev = out
            for ii, lat in enumerate(flops):
                # layer: i | scale: ii
                if ii == 0:
                    # only from keep
                    if lat[0] is not None: flops_total[ii][0] = flops_total[ii][0] + lat[0]
                    if lat[1] is not None: flops_total[ii][1] = flops_total[ii][0] + lat[1]
                else:
                    if i == ii:
                        # only from down
                        if lat[0] is not None: flops_total[ii][0] = flops_total[ii-1][1] + lat[0]
                        if lat[1] is not None: flops_total[ii][1] = flops_total[ii-1][1] + lat[1]
                    else:
                        if lat[0] is not None: flops_total[ii][0] = mjus[j][i-j-1][1] * flops_total[ii][0] + mjus[j][i-j-1][0] * flops_total[ii-1][1] + lat[0]
                        if lat[1] is not None: flops_total[ii][1] = mjus[j][i-j-1][1] * flops_total[ii][0] + mjus[j][i-j-1][0] * flops_total[ii-1][1] + lat[1]
        ###################################
        flops0 = flops_total[0][0]
        flops1 = flops_total[1][0]
        flops2 = flops_total[2][0]
        flops = sum([flops0, flops1, flops2])
        return flops
        ###################################
    def _loss(self, input, target, pretrain=False):
        loss = 0
        if pretrain is not True:
            # "random width": sampled by gambel softmax
            self.prun_mode = None
            for idx in range(len(self._arch_names)):
                #self.arch_idx = idx
                logits = self(input)
                loss = loss + sum(self._criterion(logit, target) for logit in logits)
        if len(self._width_mult_list) > 1:
            self.prun_mode = "max"

            logits = self(input)
            loss = loss + sum(self._criterion(logit, target) for logit in logits)
            self.prun_mode = "min"
            logits = self(input)
            loss = loss + sum(self._criterion(logit, target) for logit in logits)
            if pretrain == True:
                self.prun_mode = "random"
                logits = self(input)
                loss = loss + sum(self._criterion(logit, target) for logit in logits)
                self.prun_mode = "random"
                logits = self(input)
                loss = loss + sum(self._criterion(logit, target) for logit in logits)
        elif pretrain == True and len(self._width_mult_list) == 1:
            self.prun_mode = "max"
            logits = self(input)
            loss = loss + sum(self._criterion(logit, target) for logit in logits)
        return loss

    def _build_arch_parameters(self, idx):
        num_ops = len(PRIMITIVES)

        # define names
        fais = [ "fai_"+str(idx)+"_"+str(scale) for scale in [0, 1, 2] ]
        mjus = [ "beta_"+str(idx)+"_"+str(scale) for scale in [1, 2] ]

        setattr(self, fais[0], nn.Parameter(Variable(1e-3*torch.ones(self._layers, num_ops), requires_grad=True)))
        setattr(self, fais[1], nn.Parameter(Variable(1e-3*torch.ones(self._layers-1, num_ops), requires_grad=True)))
        setattr(self, fais[2], nn.Parameter(Variable(1e-3*torch.ones(self._layers-2, num_ops), requires_grad=True)))
        # mjus are now in-degree probs
        # 0: from down; 1: from keep
        setattr(self, mjus[0], nn.Parameter(Variable(1e-3*torch.ones(self._layers-2, 2), requires_grad=True)))
        setattr(self, mjus[1], nn.Parameter(Variable(1e-3*torch.ones(self._layers-3, 2), requires_grad=True)))

        thetas = [ "ratio_"+str(idx)+"_"+str(scale) for scale in [0, 1, 2] ]
        if self._prun_modes[idx] == 'arch_ratio':
            # prunning ratio
            num_widths = len(self._width_mult_list)
        else:
            num_widths = 1
        setattr(self, thetas[0], nn.Parameter(Variable(1e-3*torch.ones(self._layers-1, num_widths), requires_grad=True)))
        setattr(self, thetas[1], nn.Parameter(Variable(1e-3*torch.ones(self._layers-1, num_widths), requires_grad=True)))
        setattr(self, thetas[2], nn.Parameter(Variable(1e-3*torch.ones(self._layers-2, num_widths), requires_grad=True)))





        return {"fais": fais, "mjus": mjus, "thetas": thetas}, [getattr(self, name) for name in fais] + [getattr(self, name) for name in mjus] + [getattr(self, name) for name in thetas]

    def _reset_arch_parameters(self, idx):
        num_ops = len(PRIMITIVES)
        if self._prun_modes[idx] == 'arch_ratio':
            # prunning ratio
            num_widths = len(self._width_mult_list)
        else:
            num_widths = 1

        getattr(self, self._arch_names[idx]["fais"][0]).data = Variable(1e-3*torch.ones(self._layers, num_ops), requires_grad=True)
        getattr(self, self._arch_names[idx]["fais"][1]).data = Variable(1e-3*torch.ones(self._layers-1, num_ops), requires_grad=True)
        getattr(self, self._arch_names[idx]["fais"][2]).data = Variable(1e-3*torch.ones(self._layers-2, num_ops), requires_grad=True)
        getattr(self, self._arch_names[idx]["mjus"][0]).data = Variable(1e-3*torch.ones(self._layers-2, 2), requires_grad=True)
        getattr(self, self._arch_names[idx]["mjus"][1]).data = Variable(1e-3*torch.ones(self._layers-3, 2), requires_grad=True)
        getattr(self, self._arch_names[idx]["thetas"][0]).data = Variable(1e-3*torch.ones(self._layers-1, num_widths), requires_grad=True)
        getattr(self, self._arch_names[idx]["thetas"][1]).data = Variable(1e-3*torch.ones(self._layers-1, num_widths), requires_grad=True)
        getattr(self, self._arch_names[idx]["thetas"][2]).data = Variable(1e-3*torch.ones(self._layers-2, num_widths), requires_grad=True)
        #getattr(self, self._arch_names[idx]["log_latency"][0]).data = Variable(torch.zeros((1,), requires_grad=True))
        #getattr(self, self._arch_names[idx]["log_flops"][0]).data = Variable(torch.zeros((1,), requires_grad=True))
