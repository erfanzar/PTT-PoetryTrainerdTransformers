import argparse
import logging
import os

USE_JIT = '1'
os.environ['USE_JIT'] = USE_JIT
from transformers import AutoTokenizer, PreTrainedTokenizer
from torch.utils.data import DataLoader
from modules.datasets import CasualLMDataset
from utils.utils import get_data
from modules import LGemModelForCasualLM, LGemConfig
from core.jax_trainer import train

pars = argparse.ArgumentParser()

pars.add_argument('--batch', '--batch', type=int, default=1)
pars.add_argument('--train', '--train', type=bool, default=True)
pars.add_argument('--compile', '--compile', type=bool, default=True)
pars.add_argument('--weight', '--weight', type=str, default=None)
pars.add_argument('--accumulate', '--accumulate', type=int, default=4)
pars.add_argument('--out-path', '--out-path', type=str, default='out')
pars.add_argument('--model', '--model', type=str, default='LGeM-S')
pars.add_argument('--save-on-step', '--save-on-step', type=int, default=5000)
pars.add_argument('--data-src', '--data-src', type=str, default='data/alpaca_data.json')
# HF-kilt_tasks//eli5
options = pars.parse_args()

logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.WARN)


def main(opt):
    tokenizer: PreTrainedTokenizer = AutoTokenizer.from_pretrained('tokenizer_model/BASE')
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.pad_token_id = tokenizer.eos_token_id
    data = get_data(opt.data_src)[:5000]
    conf: LGemConfig = LGemConfig(
        hidden_size=768,
        intermediate_size=768 * 7,
        num_hidden_layers=6,
        num_attention_heads=8,
        vocab_size=32000,
    )
    model = LGemModelForCasualLM(conf)
    # Replace with your own Dataset
    dataloader = DataLoader(
        CasualLMDataset(data=data, max_length=conf.max_sequence_length, tokenizer=tokenizer, return_tensors='np'),
        batch_size=1, shuffle=True)
    print(model)
    train(model=model, config=conf,
          data_loader=dataloader, total=5000)


if __name__ == "__main__":
    main(options)
