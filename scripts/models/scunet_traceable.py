# SPDX-License-Identifier: GPL-3.0-only
"""Make SCUNet traceable for Core ML without changing what it computes.

Three things in the published network are fine in PyTorch but cannot survive
the Core ML converter, and all three are avoidable without touching the maths:

1. `einops.rearrange` builds its recipe from Python set operations inside
   `forward`. `torch.export` cannot trace that, and `torch.jit.trace` records
   the resulting shape arithmetic as hundreds of integer-cast nodes. Replaced
   with the equivalent native reshape/permute.
2. The relative position embedding is rebuilt on every forward by indexing a
   parameter with a constructed index tensor. With weights frozen it is a
   constant, so it is computed once.
3. The shifted-window mask is a boolean tensor built by in-place slice
   assignment, which Core ML cannot express. Replaced with a precomputed
   additive mask holding -inf where the boolean mask was True; adding -inf
   and filling with -inf give the same softmax.

The conversion script asserts the patched network is bit-identical to the
published one before it converts anything, so a mistake here fails loudly
instead of shipping a subtly different denoiser.

Conversion-time only. None of this ships in the application.
"""

from __future__ import annotations

import torch
import torch.nn as nn


def _generate_mask(self, h, w, p, shift):
    """'w1 w2 p1 p2 p3 p4 -> 1 1 (w1 w2) (p1 p2) (p3 p4)', einops-free."""
    attn_mask = torch.zeros(h, w, p, p, p, p, dtype=torch.bool,
                            device=self.relative_position_params.device)
    if self.type == "W":
        return attn_mask
    s = p - shift
    attn_mask[-1, :, :s, :, s:, :] = True
    attn_mask[-1, :, s:, :, :s, :] = True
    attn_mask[:, -1, :, :s, :, s:] = True
    attn_mask[:, -1, :, s:, :, :s] = True
    return attn_mask.reshape(1, 1, h * w, p * p, p * p)


def _wmsa_forward(self, x):
    p = self.window_size
    if self.type != "W":
        x = torch.roll(x, shifts=(-(p // 2), -(p // 2)), dims=(1, 2))

    b, height, width, c = x.shape
    h_windows = height // p
    w_windows = width // p
    windows = h_windows * w_windows
    pixels = p * p

    # 'b (w1 p1) (w2 p2) c -> b w1 w2 p1 p2 c -> b (w1 w2) (p1 p2) c'
    x = x.reshape(b, h_windows, p, w_windows, p, c)
    x = x.permute(0, 1, 3, 2, 4, 5).reshape(b, windows, pixels, c)

    qkv = self.embedding_layer(x)
    # 'b nw np (threeh c) -> threeh b nw np c'
    qkv = qkv.reshape(b, windows, pixels, 3 * self.n_heads, self.head_dim)
    q, k, v = qkv.permute(3, 0, 1, 2, 4).chunk(3, dim=0)

    sim = torch.einsum("hbwpc,hbwqc->hbwpq", q, k) * self.scale
    # 'h p q -> h 1 1 p q'
    sim = sim + self.relative_embedding().unsqueeze(1).unsqueeze(2)
    if self.type != "W":
        sim = sim + self._cached_additive_mask

    probs = nn.functional.softmax(sim, dim=-1)
    output = torch.einsum("hbwij,hbwjc->hbwic", probs, v)
    # 'h b w p c -> b w p (h c)'
    output = output.permute(1, 2, 3, 0, 4)
    output = output.reshape(b, windows, pixels, self.n_heads * self.head_dim)
    output = self.linear(output)
    # 'b (w1 w2) (p1 p2) c -> b (w1 p1) (w2 p2) c'
    out_c = output.shape[-1]
    output = output.reshape(b, h_windows, w_windows, p, p, out_c)
    output = output.permute(0, 1, 3, 2, 4, 5)
    output = output.reshape(b, h_windows * p, w_windows * p, out_c)

    if self.type != "W":
        output = torch.roll(output, shifts=(p // 2, p // 2), dims=(1, 2))
    return output


def _conv_trans_forward(self, x):
    conv_x, trans_x = torch.split(self.conv1_1(x),
                                  (self.conv_dim, self.trans_dim), dim=1)
    conv_x = self.conv_block(conv_x) + conv_x
    trans_x = trans_x.permute(0, 2, 3, 1)      # 'b c h w -> b h w c'
    trans_x = self.trans_block(trans_x)
    trans_x = trans_x.permute(0, 3, 1, 2)      # 'b h w c -> b c h w'
    return x + self.conv1_2(torch.cat((conv_x, trans_x), dim=1))


def apply(module, example: torch.Tensor) -> int:
    """Patch a loaded SCUNet so it can be traced. Returns blocks patched.

    Each attention stage works at its own resolution, so the shifted-window
    mask cannot be derived from the network input size alone. Rather than
    reproduce the downsampling arithmetic here, one real forward pass records
    the shape every attention block actually sees.
    """
    from network_scunet import WMSA, ConvTransBlock

    seen: dict[int, tuple[int, int]] = {}
    handles = []
    for child in module.modules():
        if isinstance(child, WMSA):
            def record(mod, args, key=id(child)):
                seen[key] = (int(args[0].shape[1]), int(args[0].shape[2]))
            handles.append(child.register_forward_pre_hook(record))
    with torch.no_grad():
        module(example)
    for handle in handles:
        handle.remove()

    patched = 0
    for child in module.modules():
        if isinstance(child, WMSA):
            with torch.no_grad():
                embedding = child.relative_embedding().detach().clone()
            child.register_buffer("_cached_relative_embedding", embedding,
                                  persistent=False)
            child.relative_embedding = (
                lambda self=child: self._cached_relative_embedding)
            if child.type != "W":
                height, width = seen[id(child)]
                p = child.window_size
                with torch.no_grad():
                    bool_mask = _generate_mask(child, height // p, width // p,
                                               p, shift=p // 2)
                    additive = torch.zeros(bool_mask.shape,
                                           dtype=torch.float32)
                    additive = additive.masked_fill(bool_mask, float("-inf"))
                child.register_buffer("_cached_additive_mask", additive,
                                      persistent=False)
            child.forward = _wmsa_forward.__get__(child, WMSA)
            child.generate_mask = _generate_mask.__get__(child, WMSA)
            patched += 1
        elif isinstance(child, ConvTransBlock):
            child.forward = _conv_trans_forward.__get__(child, ConvTransBlock)
            patched += 1
    return patched
