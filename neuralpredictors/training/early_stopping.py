import copy
import logging
from collections import OrderedDict

import numpy as np
import torch

logger = logging.getLogger(__name__)


def copy_state(model):
    """
    Given PyTorch module `model`, makes a copy of the state onto CPU.
    Args:
        model: PyTorch module to copy state dict of

    Returns:
        A copy of state dict with all tensors allocated on the CPU
    """
    copy_dict = OrderedDict()
    state_dict = model.state_dict()
    for k, v in state_dict.items():
        if torch.is_tensor(v):
            copy_dict[k] = v.cpu() if v.is_cuda else v.clone()
        else:
            copy_dict[k] = copy.deepcopy(v)

    return copy_dict


def early_stopping(
    model,
    objective,
    interval=5,
    patience=20,
    start=0,
    max_iter=1000,
    maximize=True,
    tolerance=1e-5,
    switch_mode=True,
    restore_best=True,
    tracker=None,
    scheduler=None,
    lr_decay_steps=1,
    number_warmup_epochs=0,
):
    """
    Early stopping iterator. Keeps track of the best model state during training. Resets the model to its
        best state, when either the number of maximum epochs or the patience [number of epochs without improvement)
        is reached.
    Also includes a convenient way to reduce the learning rate. Takes as an additional input a PyTorch scheduler object
        (e.g. torch.optim.lr_scheduler.ReduceLROnPlateau), which will automatically decrease the learning rate.
        If the patience counter is reached, the scheduler will decay the LR, and the model is set back to its best state.
        This loop will continue for n times in the variable lr_decay_steps. The patience and tolerance parameters in
        early stopping and the scheduler object should be identical.


    Args:
        model:     model that is being optimized
        objective: objective function that is used for early stopping. The function must accept single positional argument `model`
            and return a single scalar quantity.
        interval:  interval at which objective is evaluated to consider early stopping
        patience:  number of continuous epochs the objective could remain without improvement before the iterator terminates
        start:     start value for iteration (used to check against `max_iter`)
        max_iter:  maximum number of iterations before the iterator terminated
        maximize:  whether the objective is maximized of minimized
        tolerance: margin by which the new objective score must improve to be considered as an update in best score
        switch_mode: whether to switch model's train mode into eval prior to objective evaluation. If True (default),
                     the model is switched to eval mode before objective evaluation and restored to its previous mode
                     after the evaluation.
        restore_best: whether to restore the best scoring model state at the end of early stopping
        tracker (Tracker):
            Tracker to be invoked for every epoch. `log_objective` is invoked with the current value of `objective`. Note that `finalize`
            method is NOT invoked.
        scheduler:  scheduler object or tuple of two scheduler objects, which automatically modifies the LR by a specified amount.
                    If a tuple of schedulers is provided the 1st scheduler is assumed to be the warm up scheduler. The .step method
                    for the 1st scheduler will be called while epoch is smaller than number_warmup_epochs afterwards the .step method of
                    the second scheduler is called. The current value of `objective` is passed to the `step` method if the scheduler at hand is `ReduceLROnPlateau`.
                    For example a provided tuple of schedulers can be of the form:

                                 scheduler = (warmup_scheduler,CosineAnnealingLR(*args,**kwargs))

                    or in case that no scheduler is desired after the warm up:

                                 scheduler = (warmup_scheduler,None).

                    An example warm up scheduler can be defined as:

                                def warmup_function(current_step: int):
                                    return 1 / (2 ** (float(number_warmup_epochs - current_step - 1)))

                                warmup_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=warmup_function)

                    Of course single schedulers can also be provided.
                    If the warm-up is shifted (goes to a to high learning rate or does not reach the desired learning rate),
                    consider adjusting the warm up function accordingly.
        lr_decay_steps: Number of times the learning rate should be reduced before stopping the training.
        number_warmup_epochs: Number of warm-up epochs
    """
    training_status = model.training

    def _objective():
        if switch_mode:
            model.eval()
        ret = objective(model)
        if switch_mode:
            model.train(training_status)
        return ret

    def decay_lr(model, best_state_dict):
        old_objective = _objective()
        if restore_best:
            model.load_state_dict(best_state_dict)
            logger.info(f"Restoring best model after lr decay! {old_objective:.6f} ---> {_objective():.6f}")

    def finalize(model, best_state_dict):
        old_objective = _objective()
        if restore_best:
            model.load_state_dict(best_state_dict)
            logger.info(f"Restoring best model! {old_objective:.6f} ---> {_objective():.6f}")
        else:
            logger.info(f"Final best model! objective {_objective():.6f}")

    epoch = start
    # turn into a sign
    maximize = -1 if maximize else 1
    best_objective = current_objective = _objective()
    best_state_dict = copy_state(model)

    # check if the learning rate scheduler is 'ReduceLROnPlateau' so that we pass the current_objective to step
    reduce_lr_on_plateau = False
    if isinstance(scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
        reduce_lr_on_plateau = True
    elif isinstance(scheduler, tuple):
        if isinstance(scheduler[1], torch.optim.lr_scheduler.ReduceLROnPlateau):
            reduce_lr_on_plateau = True

    # check if warm up is to be performed
    if isinstance(scheduler, tuple):
        warmup = True

        # check if the warm-up scheduler is not of type None
        if scheduler[0] is None:
            logger.warning(
                f"Provided warm up scheduler is of type None. Warm up epochs set to {number_warmup_epochs}. Setting number of warm up epochs to 0"
            )
            number_warmup_epochs = 0
    else:
        warmup = False

    # check if warm up scheduler and number of warm-up epochs is provided
    if warmup and number_warmup_epochs == 0:
        logger.warning("Warm up scheduler is provided, but number of warm up steps is set to 0")

    # inform user that no warm-up scheduler is provided althouth warm-up epochs is non zero
    elif not warmup and number_warmup_epochs > 0:
        logger.warning(
            f"Number of warm up steps is set to {number_warmup_epochs}, but no warm up scheduler is provided"
        )

    for repeat in range(lr_decay_steps):
        patience_counter = 0

        while patience_counter < patience and epoch < max_iter:
            for _ in range(interval):
                epoch += 1
                if tracker is not None:
                    tracker.log_objective(current_objective)
                if (~np.isfinite(current_objective)).any():
                    logger.warning("Objective is not Finite. Stopping training")
                    finalize(model, best_state_dict)
                    return
                yield epoch, current_objective

            current_objective = _objective()

            # if a scheduler is defined, a .step with or without the current objective is all that is needed to reduce the LR
            if scheduler is not None:
                if warmup and epoch < number_warmup_epochs:
                    # warm-up step
                    scheduler[0].step()
                elif reduce_lr_on_plateau:
                    # reduce_lr_on_plateau requires current objective for the step
                    if not warmup:
                        scheduler.step(current_objective)
                    else:
                        scheduler[1].step(current_objective)
                else:
                    # .step() for the rest of the schedulers
                    if not warmup:
                        scheduler.step()
                    else:
                        if scheduler[1] is not None:
                            scheduler[1].step()

            if current_objective * maximize < best_objective * maximize - tolerance:
                logger.info(f"[{epoch:03d}|{patience_counter:02d}/{patience:02d}] ---> {current_objective}")
                best_state_dict = copy_state(model)
                best_objective = current_objective
                patience_counter = 0
            else:
                patience_counter += 1
                logger.info(f"[{epoch:03d}|{patience_counter:02d}/{patience:02d}] ---> {current_objective}")

        if (epoch < max_iter) & (lr_decay_steps > 1) & (repeat < lr_decay_steps):
            decay_lr(model, best_state_dict)

    finalize(model, best_state_dict)
