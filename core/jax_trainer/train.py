import os
from pathlib import Path
from typing import Optional, Union

import erutils
import flax
import jax
import numpy as np
import optax
import torch.utils.data
from erutils.loggers import show_hyper_parameters
from flax.training import checkpoints
from jax import numpy as jnp
from jax.random import PRNGKey
from torch.utils.tensorboard import SummaryWriter
from tqdm.auto import tqdm

from config.config import TQDM_KWARGS
from modules import LGemConfig, LGemModelForCasualLM
from utils.utils import cross_entropy_loss, flax_count_params


def train(model: Union[flax.linen.Module, LGemModelForCasualLM], config: Optional[LGemConfig],
          data_loader: torch.utils.data.DataLoader, params=None, seed=None, dummy_input=None, total=None,
          ckpt_dir: Union[os.PathLike, str] = 'out/ckpt', auto_init: bool = True
          ):
    def make2d(tensor) -> jnp.DeviceArray:
        return tensor.reshape(-1, tensor.shape[-1])

    show_hyper_parameters(config)
    key = PRNGKey(np.random.randint(0, 1e9) if seed is None else seed)
    opt = optax.adamw(learning_rate=config.learning_rate)
    ckpt_dir = Path(ckpt_dir)

    ckpt_dir.mkdir(exist_ok=True)
    board = SummaryWriter(ckpt_dir / 'tensorboard')
    if params is None:
        erutils.fprint('No Parameters for model found [STATE INITIALIZING]')
        if dummy_input is not None:
            inp = dummy_input
        elif dummy_input is None and auto_init is True:
            inp = np.random.randint(0, config.vocab_size, (1, 128))
        else:
            raise ValueError
        erutils.fprint('INITIALIZING MODEL PARAMETER ! ')
        params = jax.jit(model.init)(key, inp)

    erutils.fprint(f'MODEL UP WITH {flax_count_params(params) / 1e6} M PARAMETERS')
    jit_model = jax.jit(model.apply)

    def step(_params, _input_ids, _attention_mask):
        prediction = jit_model(_params, _input_ids, _attention_mask)
        __loss = cross_entropy_loss(targets=_input_ids[:, 1:], prediction=prediction[:, :-1])
        return __loss

    graded_step = jax.value_and_grad(step)
    jit_update = jax.jit(opt.update)
    jit_apply_update = jax.jit(optax.apply_updates)

    def train_step(_params, _input_ids, _attention_mask, _state_opt):

        _loss, grads = graded_step(_params, _input_ids, _attention_mask)
        del _input_ids, _attention_mask
        updates, _state_opt = jit_update(updates=grads, state=_state_opt, params=_params)
        _params = jit_apply_update(_params, updates)
        return _params, _state_opt, _loss

    erutils.fprint('INITIALIZING OPTIMIZER STATE ! ')
    opt_state = opt.init(params)
    # erutils.fprint('START TRAINING *')
    jit_train_step = jax.jit(train_step)
    at = 0
    for epoch in range(config.epochs):
        pbar = tqdm(enumerate(data_loader), total=total, **TQDM_KWARGS)
        avg_loss = 0
        for i, (input_ids, attention_mask) in pbar:

            params, opt_state, loss = jit_train_step(params, make2d(input_ids).cpu().numpy(),
                                                     make2d(attention_mask).cpu().numpy(), opt_state)
            avg_loss += loss
            at += 1
            pbar.set_postfix(loss=loss, average_loss=avg_loss / (i + 1))
            if (at % 10) == 0:
                board.add_scalar('train/Loss', scalar_value=np.asarray(loss), global_step=at)
                board.add_scalar('train/avg-Loss', scalar_value=(np.asarray(avg_loss) / (i + 1)),
                                 global_step=at)
                board.add_scalar('train/Speed', scalar_value=at, global_step=at)
                board.add_scalar('train/epoch', scalar_value=epoch, global_step=at)

        checkpoints.save_checkpoint(ckpt_dir=ckpt_dir, target={'params': params, "opt_state": opt_state}, step=epoch,
                                    keep=2, overwrite=True)
