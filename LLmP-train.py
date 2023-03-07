import argparse
import logging
import math
import typing
from typing import Optional, Union

import erutils
import torch.utils.data
from datasets import load_dataset
from erutils.loggers import fprint
from torch import Tensor
from torch.utils.tensorboard import SummaryWriter
from tqdm.auto import tqdm
from transformers import GPT2Tokenizer

from modules.dataset import DatasetLLmP, Tokens
from modules.models import LLmP, LLmPConfig
from utils.utils import make2d, save_checkpoints, get_config_by_name, device_info

torch.manual_seed(42)
torch.backends.cudnn.benchmark = True

pars = argparse.ArgumentParser()

pars.add_argument('--batch', '--batch', type=int, default=3)
pars.add_argument('--train', '--train', type=bool, default=True)
pars.add_argument('--compile', '--compile', type=bool, default=True)
pars.add_argument('--load', '--load', type=bool, default=True)
pars.add_argument('--model', '--model', type=str, default='LLmP')
pars.add_argument('--data-src', '--data-src', type=str, default='data/TPAP.txt')

options = pars.parse_args()

logger = logging.getLogger(__name__)
# logging.basicConfig(level=logging.WARN)


def train(input_ids: Optional[Tensor],
          targets: Optional[Tensor],
          attention_mask: Optional[Tensor],
          network: Optional[LLmP.forward],
          optim: Optional[torch.optim.AdamW],
          loss_average: Optional[Tensor],
          device: Union[torch.device, str]) -> [typing.Union[torch.Tensor],
                                                typing.Union[torch.Tensor]]:
    labels: Optional[Tensor] = make2d(targets.type(torch.long).to(device))
    input_ids: Optional[Tensor] = make2d(input_ids.type(torch.long).to(device))
    network.zero_grad(set_to_none=True)
    _, loss = network(input_ids=input_ids, labels=labels, attention_mask=attention_mask)

    loss_average += loss.item()
    loss.backward()
    optim.step()
    return loss, loss_average


def main(opt):
    device_info()
    if not opt.data_src.startswith('HF-'):
        data = open(opt.data_src, 'r', encoding='utf8').read().split()
    else:
        name = opt.data_src.replace('HF-', '')
        if '/' in name:
            model_name = name.split('/')
            data = load_dataset(model_name[0], model_name[1])
        else:
            data = load_dataset(name)
        data = data["train"]['text']
        selected = int(len(data) * 0.01)
        data = data[:selected]
    parameters: LLmPConfig = get_config_by_name(opt.model)
    tokenizer: GPT2Tokenizer = GPT2Tokenizer.from_pretrained('gpt2', bos_token=Tokens.eos,
                                                             pad_token=Tokens.pad, sos_token=Tokens.sos)
    dataset = DatasetLLmP(data=data, max_length=parameters.max_sentence_length, tokenizer=tokenizer)
    parameters.vocab_size = dataset.tokenizer.vocab_size
    parameters.vocab_size += 1
    # parameters.device = 'cpu'
    parameters.data_path = opt.data_src

    parameters.batch_size = opt.batch
    dataloader = torch.utils.data.DataLoader(dataset=dataset, batch_size=parameters.batch_size, num_workers=4,
                                             pin_memory=True)
    erutils.loggers.show_hyper_parameters(parameters)

    fprint('Loading Model ...' if opt.load else 'Creating Model ...')

    model = LLmP(config=parameters).to(parameters.device) if opt.load else LLmP(config=parameters).to('cpu')
    optimizer_kwargs = dict(lr=parameters.lr, weight_decay=parameters.weight_decay)
    optimizer = torch.optim.AdamW(model.parameters(), **optimizer_kwargs)
    model_parameters_size: typing.Optional[float] = sum(p.numel() for p in model.parameters()) / 1e6

    checkpoints = torch.load(f'{opt.model}-model.pt', 'cpu') if opt.load else None

    if checkpoints is not None:
        model.load_state_dict(checkpoints['model'])
        model = model.to(parameters.device)
        optimizer.load_state_dict(checkpoints['optimizer'])
    fprint(
        f'Model Loaded With {model_parameters_size} Million Parameters' if opt.load
        else f'Model Created With {model_parameters_size} Million Parameters')

    if opt.compile:
        model = torch.compile(model)
        fprint(f"Model Compiled Successfully")
    board = SummaryWriter(log_dir='out/', filename_suffix='LLmP')
    question = dataset.encode(Tokens.sos + 'say something ').to(parameters.device)
    question = question['input_ids'].to(parameters.device)
    model = model.to(device=parameters.device)
    logger.info('TRAIN IS ABOUT TO START!!!')
    if opt.train:
        logger.info('TRAIN IS ABOUT TO START')
        for epoch in range(checkpoints['epoch'] if opt.load else 0, parameters.epochs):
            loss_avg = 0
            with tqdm(enumerate(dataloader), colour='blue',
                      total=math.ceil(dataset.__len__() // parameters.batch_size)) as progress_bar:
                for i, (input_ids_t, attention_mask) in progress_bar:
                    loss, loss_avg = train(input_ids=input_ids_t, targets=input_ids_t, network=model, optim=optimizer,
                                           loss_average=loss_avg, device=parameters.device,
                                           attention_mask=attention_mask)
                    if i % 50 == 0:
                        board.add_scalar('train/Loss', scalar_value=loss.item(), global_step=i * (epoch + 1))
                        board.add_scalar('train/avg-Loss', scalar_value=(loss_avg / (i + 1)),
                                         global_step=i * (epoch + 1))
                    progress_bar.set_postfix(epoch=f'[{epoch}/{parameters.epochs}]', device=parameters.device,
                                             loss_avg=(loss_avg / (i + 1)),
                                             loss=loss.item())

                print()
                save_checkpoints(model=model.state_dict(), optimizer=optimizer.state_dict(),
                                 epochs=parameters.epochs,
                                 epoch=epoch + 1, config=opt.model,
                                 name=f'{opt.model}-model.pt')
                progress_bar.write('==> MODEL SAVED SUCCESSFULLY')
                # predictions = model.generate(prompts=question, max_gen_len=30, pad_id=dataset.tokenizer.pad_token_id,
                #                              eos_id=dataset.tokenizer.eos_token_id)
                # progress_bar.write(f'QUESTION : {dataset.tokenizer.decode(question[0])}')
                # progress_bar.write(f'PREDICTION : {dataset.tokenizer.decode(predictions)}')


if __name__ == "__main__":
    main(options)