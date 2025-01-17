import flax.core
import jax
import optax
from functools import partial
from datasets import load_dataset
from tqdm.auto import tqdm
from fjutils.optimizers import get_adamw_with_cosine_scheduler
from dataclasses import dataclass, field
from jax.random import PRNGKey, split
import re
import numpy as np
from jax.sharding import PartitionSpec as PS
from jax.experimental.pjit import pjit
from torch.utils.data import DataLoader
from typing import Optional
from transformers import HfArgumentParser, AutoConfig, FlaxAutoModelForCausalLM, AutoTokenizer, FlaxPreTrainedModel, \
    logging

from jax import numpy as jnp
from jax.experimental import mesh_utils
from jax.sharding import Mesh

from flax.training import train_state
from utils.utils import with_sharding_constraint

# from utils.timer import Timers

logging.set_verbosity_error()
# jax.config.update()
# jax.default_device = jax.devices('cpu')[0]

DEFAULT_PAD_TOKEN = '<|endoftext|>'
DEFAULT_EOS_TOKEN = '<|endoftext|>'
DEFAULT_BOS_TOKEN = '<|endoftext|>'


def get_dtype(dtype_str):
    return {
        'bf16': jnp.bfloat16,
        'bfloat16': jnp.bfloat16,
        'fp16': jnp.float16,
        'float16': jnp.float16,
        'fp32': jnp.float32,
        'float32': jnp.float32,
        'fp64': jnp.float64,
        'float64': jnp.float64,
    }[dtype_str]


def prefix_printer(prefix, value):
    print(f' \033[1;31m{prefix}\033[1;0m : {value}')


def get_model_devices(params) -> [str]:
    devi = {}
    for pd in jax.tree_util.tree_flatten(flax.core.unfreeze(params))[0]:
        if isinstance(pd, jnp.DeviceArray):
            devi[pd.device()] = 'Using'
    return list(devi.keys())


def check_device():
    print('Device Checking in order ...')
    dev = {
        'CPU': jax.devices('cpu'),
    }
    none_val = 'NOT found'
    try:
        dev = dev | dict(TPU=jax.devices('tpu'))
    except RuntimeError:

        dev['TPU'] = none_val

    try:
        dev = dev | dict(GPU=jax.devices('gpu'))
    except RuntimeError:

        dev['GPU'] = none_val
    print(f'founded Accelerators on this device are ')

    for k, v in dev.items():
        prefix_printer(k, v)
    gpu = dev['GPU']
    tpu = dev['TPU']
    prefix_printer('Device Report', f'This machine Contain {len(dev["CPU"])}'
                                    f' CPUS , {f"have {len(gpu)} GPUS " if gpu != 0 else "have No GPU"} and '
                                    f'{f"have {len(tpu)} TPU Core" if tpu != none_val else "have No TPU Core"}')
    return dev


@dataclass
class TrainingArguments:
    output_dir: str = field(
        default='OST/JaxModel',
        metadata={"help": "The output directory where the model predictions and checkpoints will be written."},
    )
    overwrite_output_dir: bool = field(
        default=False,
        metadata={
            "help": (
                "Overwrite the content of the output directory. "
                "Use this to continue training if output_dir points to a checkpoint directory."
            )
        },
    )
    per_device_batch_size: int = field(
        default=8, metadata={"help": "Batch size per GPU/TPU core/CPU for training."}
    )

    learning_rate: float = field(default=1e-4, metadata={"help": "The initial learning rate for AdamW."})

    weight_decay: float = field(default=0.0, metadata={"help": "Weight decay for AdamW if we apply some."})

    adam_beta1: float = field(default=0.9, metadata={"help": "Beta1 for AdamW optimizer"})

    adam_beta2: float = field(default=0.999, metadata={"help": "Beta2 for AdamW optimizer"})

    adam_epsilon: float = field(default=1e-8, metadata={"help": "Epsilon for AdamW optimizer."})

    num_train_epochs: int = field(default=3, metadata={"help": "Total number of training epochs to perform."})

    logging_steps: int = field(default=500, metadata={"help": "Log every X updates steps."})
    save_steps: int = field(default=500, metadata={"help": "Save checkpoint every X updates steps."})

    seed: int = field(default=42, metadata={"help": "Random seed that will be set at the beginning of training."})

    push_to_hub: bool = field(
        default=False, metadata={"help": "Whether or not to upload the trained model to the model hub after training."}
    )

    hub_model_id: str = field(
        default=None, metadata={"help": "The name of the repository to keep in sync with the local `output_dir`."}
    )

    hub_token: str = field(default=None, metadata={"help": "The token to use to push to the Model Hub."})


@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(
        default=None,
        metadata={
            "help":
                "The model checkpoint for weights initialization.Don't set if you want to train a model from scratch."

        },
    )

    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path "}
    )
    cache_dir: Optional[str] = field(
        default=None, metadata={"help": "Where do you want to store the pretrained models downloaded from s3"}
    )
    dtype: Optional[str] = field(
        default="float32",
        metadata={
            "help": (
                "Floating-point format in which the model weights should be initialized and trained. Choose one of"
                " `[float32, float16, bfloat16]`."
            )
        },
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": (
                "Will use the token generated when running `huggingface-cli login` (necessary to use this script "
                "with private models)."
            )
        },
    )
    trust_remote_code: bool = field(
        default=False,
        metadata={
            "help": "Trust remote code or Not ? "
        },
    )

    smart_chooses: bool = field(
        default=True,
        metadata={
            "help": "smart_chooses to skip some known errors"
        },
    )


@dataclass
class DataTrainingArguments:
    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    dataset_field: Optional[str] = field(
        default=None, metadata={"help": "The field in data set to run tokenizer on that field"}
    )

    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of training examples to this "
                "value if set."
            )
        },
    )

    block_size: int = field(
        default=2048,
        metadata={
            "help": (
                "Optional input sequence length after tokenization. "
                "The training dataset will be truncated in block of this size for training. "
                "Default to the model max input length for single sentence inputs (take into account special tokens)."
            )
        },
    )
    preprocessing_num_workers: Optional[int] = field(
        default=1,
        metadata={"help": "The number of processes to use for the preprocessing."},
    )


def main():
    assert jax.device_count() % 2 == 0, 'In order to use this script you need to use this script on a device with' \
                                        ' at least 2 devices and device numbers must be divisible by 2'
    prefix_printer('Attention ',
                   'Theres a chance that if you running on the Kaggle TPU vm 3-8 TPUs'
                   ' are being used but not showing up in logging')
    check_device()
    arguments = HfArgumentParser((TrainingArguments, ModelArguments, DataTrainingArguments))
    train_args, model_args, data_args = arguments.parse_args_into_dataclasses()
    train_args: TrainingArguments = train_args
    model_args: ModelArguments = model_args
    data_args: DataTrainingArguments = data_args

    memory_flag = {'tokenizer_name': model_args.tokenizer_name, 'model_name': model_args.model_name_or_path,
                   'config_from': model_args.config_name, 'dataset_name': data_args.dataset_name,
                   'dataset_field': data_args.dataset_field, 'block_size|max_sequence_length': data_args.block_size,
                   'num_train_epochs': train_args.num_train_epochs, 'seed': train_args.seed,
                   'hub_model_id': train_args.hub_model_id, 'save_steps': train_args.save_steps,
                   'logging_steps': train_args.logging_steps, 'learning_rate': train_args.learning_rate}

    assert model_args.tokenizer_name, 'tokenizer_name is a required field for trainer ' \
                                      'please pass a tokenizer path or repo id'
    prefix_printer('Default Device Jax', jax.default_device)

    dataset = load_dataset(data_args.dataset_name)

    if data_args.dataset_field is not None:
        def tokenize(batch):
            return tokenizer(
                batch[data_args.dataset_field],
                padding='max_length',
                max_length=data_args.block_size
            )

        dataset = dataset.map(
            tokenize,
            batched=True,
            batch_size=32,
            num_proc=data_args.preprocessing_num_workers,
            remove_columns=dataset['train'].column_names
        )

    def collate_fn(batch):
        rs = {}
        for key in batch[0].keys():
            rs[key] = jnp.stack([jnp.array(f[key]) for f in batch])
        return rs

    dataloader = DataLoader(dataset['train'],
                            collate_fn=collate_fn,
                            batch_size=train_args.per_device_batch_size * jax.device_count(),
                            num_workers=data_args.preprocessing_num_workers)

    total_iterations = int(len(dataloader) * data_args.max_train_samples)

    tokenizer = AutoTokenizer.from_pretrained(model_args.tokenizer_name, trust_remote_code=True)
    if tokenizer.pad_token is None and model_args.smart_chooses:
        prefix_printer('Extra Action ', 'No PAD token found setting default padding token')
        tokenizer.pad_token = DEFAULT_PAD_TOKEN
        tokenizer.pad_token_id = tokenizer.eos_token_id

    if tokenizer.pad_token is None and model_args.smart_chooses:
        prefix_printer('Extra Action ', 'No BOS token found setting default padding token')
        tokenizer.bos_token = DEFAULT_BOS_TOKEN
        tokenizer.bos_token_id = tokenizer.eos_token_id

    extra = dict(vocab_size=len(tokenizer.get_vocab())) if model_args.smart_chooses else {}
    config = AutoConfig.from_pretrained(model_args.config_name,
                                        hidden_size=512,
                                        intermediate_size=1024,
                                        num_attention_heads=8,
                                        trust_remote_code=True,
                                        **extra
                                        ) if model_args.config_name is not None else None
    assert len(
        tokenizer.get_vocab()) == config.vocab_size, 'Tokenizer and Vocab size are not match that will' \
                                                     ' cause of error in the training and inference process'

    if config is None:
        assert model_args.model_name_or_path
        model = FlaxAutoModelForCausalLM.from_pretrained(
            model_args.model_name_or_path,
            use_auth_token=model_args.use_auth_token,
            dtype=jnp.dtype(model_args.dtype),
            trust_remote_code=model_args.trust_remote_code,
        )
    else:
        model: flax.linen.Module | FlaxPreTrainedModel = FlaxAutoModelForCausalLM.from_config(
            config=config,
            trust_remote_code=True,
            _do_init=False
        )
    scheduler = optax.cosine_decay_schedule(
        init_value=train_args.learning_rate,
        decay_steps=total_iterations,
    )

    weight_decay: float = 2e-1
    mask = None

    tx, scheduler = get_adamw_with_cosine_scheduler(
        learning_rate=train_args.learning_rate,
        steps=total_iterations,
    )

    def init_fn():
        key = PRNGKey(0)
        key, rng = split(key)
        params = model.module.init(
            rng,
            input_ids=jnp.zeros((4, 2048), dtype=jnp.int32),
            return_dict=False
        )
        _i = jax.tree_util.tree_flatten(flax.core.unfreeze(params))[0]
        prefix_printer('Model Contain ', f'{sum(i.size for i in _i) / 1e6} Million Parameters')
        try:
            prefix_printer('Model Devices Are', f'{get_model_devices(params)}')
        except jax.errors.ConcretizationTypeError:
            prefix_printer('Model Devices Are', f'pjit is in use cant get device map')

        if model_args.dtype == 'float32':
            params = model.to_fp32(params)
        elif model_args.dtype == 'float16':
            params = model.to_fp16(params)
        elif model_args.dtype == 'bfloat16':
            params = model.to_bf16(params)
        else:
            raise ValueError(f'Wrong DataType {model_args.dtype} is not in supported list')
        return train_state.TrainState.create(
            tx=tx,
            apply_fn=model.__call__,
            params=params['params'],
        )

    def apply_model(state: train_state.TrainState, batch):
        batch = with_sharding_constraint(batch, partition_specs=PS('fsdp', 'mp'))

        def loss_fn(params):
            logits = state.apply_fn(params, **batch, return_dict=True).logits
            loss_ = optax.softmax_cross_entropy_with_integer_labels(logits=logits[..., :-1, :],
                                                                    labels=batch['input_ids'][..., 1:],
                                                                    )
            return loss_

        graf_fn = jax.value_and_grad(loss_fn, has_aux=False)
        loss, grad = graf_fn(state.params)
        state = state.apply_gradients(grads=grad)

        return state, loss

    state_shape = jax.eval_shape(init_fn)
    assert hasattr(config, 'get_partition_rules'), 'config has no attribute partition rules'

    def match_partition_rules(rules, params):
        def get_partition_spec(name, leaf):
            if len(leaf.shape) == 0 or np.prod(leaf.shape) == 1:
                return PS()
            for rule, ps in rules:
                if re.search(rule, name) is not None:
                    return ps
            raise ValueError(f'Partition rule not found for param: {name}')

        def tree_path_to_string(path):
            keys = []
            for i, key in enumerate(path):
                if isinstance(key, jax.tree_util.SequenceKey):
                    keys.append(str(key.idx))
                elif isinstance(key, (jax.tree_util.DictKey, jax.tree_util.FlattenedIndexKey)):
                    keys.append(str(key.key))
                elif isinstance(key, jax.tree_util.GetAttrKey):
                    keys.append(str(key.name))
                else:
                    keys.append(str(key))
            return '/'.join(keys)

        return jax.tree_util.tree_map_with_path(
            lambda path, p: get_partition_spec(tree_path_to_string(path), p),
            params
        )

    partition_space_state = match_partition_rules(config.get_partition_rules(), state_shape)
    sharded_init_fn = pjit(
        init_fn,
        donate_argnums=(),
        in_shardings=PS(),
        out_shardings=partition_space_state
    )
    sharded_apply_model = pjit(
        apply_model,
        in_shardings=(partition_space_state, PS()),
        out_shardings=(partition_space_state, PS())
    )

    mesh = Mesh(mesh_utils.create_device_mesh((1, len(jax.devices()), 1), ('dp', 'fsdp', 'mp')))
    with mesh:
        state = sharded_init_fn()
        total = len(dataloader) * train_args.num_train_epochs
        pbar = tqdm(total=total)
        i = 0
        for ep in range(train_args.num_train_epochs):
            for batch in dataloader:
                i += 1
                state, loss = sharded_apply_model(state=state, batch=batch)
                pbar.update(1)
                pbar.set_postfix(loss=loss, passed=i / total, learning_rate=scheduler(i).tolist(),
                                 epoch=f'[{ep}/{train_args.num_train_epochs}]')
                if i % train_args.logging_steps == 0:
                    prefix_printer(f'Loss Step {i}', loss)

                if i % train_args.save_steps == 0:
                    state.model_it_self.save_pretrained(
                        train_args.output_dir,
                        params=state.params
                    )


if __name__ == "__main__":
    main()
