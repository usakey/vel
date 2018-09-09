import attr
import numpy as np
import sys
import torch
import tqdm


import vel.util.math as math_util

from vel.api.base import Model, ModelFactory
from vel.api.metrics import AveragingNamedMetric
from vel.api.info import EpochInfo, BatchInfo
from vel.openai.baselines.common.vec_env import VecEnv
from vel.rl.reinforcers.policy_gradient.policy_gradient_base import PolicyGradientBase
from vel.rl.api.base import ReinforcerBase, ReinforcerFactory, VecEnvFactory, EnvRollerFactory, EnvRollerBase
from vel.rl.metrics import (
    FPSMetric, EpisodeLengthMetric, EpisodeRewardMetricQuantile, ExplainedVariance,
    EpisodeRewardMetric, FramesMetric
)


@attr.s(auto_attribs=True)
class PolicyGradientSettings:
    """ Settings dataclass for a policy gradient reinforcer """
    number_of_steps: int
    discount_factor: float
    batch_size: int = 256
    experience_replay: int = 1


class PolicyGradientReinforcer(ReinforcerBase):
    """ Train network using a policy gradient algorithm """
    def __init__(self, device: torch.device, settings: PolicyGradientSettings, env: VecEnv, model: Model,
                 policy_gradient: PolicyGradientBase, env_roller: EnvRollerBase) -> None:
        self.device = device
        self.settings = settings

        self.environment = env
        self._internal_model = model.to(self.device)

        self.env_roller = env_roller

        self.policy_gradient = policy_gradient

    def metrics(self) -> list:
        """ List of metrics to track for this learning process """
        my_metrics = [
            FramesMetric("frames"),
            FPSMetric("fps"),
            EpisodeRewardMetric('PMM:episode_rewards'),
            EpisodeRewardMetricQuantile('P09:episode_rewards', quantile=0.9),
            EpisodeRewardMetricQuantile('P01:episode_rewards', quantile=0.1),
            EpisodeLengthMetric("episode_length"),
            AveragingNamedMetric("advantage_norm"),
            ExplainedVariance()
        ]

        return my_metrics + self.policy_gradient.metrics()

    @property
    def model(self) -> Model:
        """ Model trained by this reinforcer """
        return self._internal_model

    def initialize_training(self):
        """ Prepare models for training """
        self.model.reset_weights()
        self.policy_gradient.initialize(self.settings, environment=self.environment, device=self.device)

    def train_epoch(self, epoch_info: EpochInfo) -> None:
        """ Train model on an epoch of a fixed number of batch updates """
        for callback in epoch_info.callbacks:
            callback.on_epoch_begin(epoch_info)

        for batch_idx in tqdm.trange(epoch_info.batches_per_epoch, file=sys.stdout, desc="Training", unit="batch"):
            batch_info = BatchInfo(epoch_info, batch_idx)

            for callback in batch_info.callbacks:
                callback.on_batch_begin(batch_info)

            self.train_batch(batch_info)

            for callback in batch_info.callbacks:
                callback.on_batch_end(batch_info)

            # Even with all the experience replay, we count the single rollout as a single batch
            epoch_info.result_accumulator.calculate(batch_info)

        epoch_info.result_accumulator.freeze_results()
        epoch_info.freeze_epoch_result()

        for callback in epoch_info.callbacks:
            callback.on_epoch_end(epoch_info)

    def train_batch(self, batch_info: BatchInfo) -> None:
        """ Single, most atomic 'step' of learning this reinforcer can perform """
        # Calculate environment rollout on the evaluation version of the model
        self.model.eval()

        rollout = self.env_roller.rollout(batch_info, self.model)

        rollout_size = rollout['observations'].size(0)
        indices = np.arange(rollout_size)

        batch_splits = math_util.divide_ceiling(rollout_size, self.settings.batch_size)

        # Perform the training step
        self.model.train()

        # All policy gradient data will be put here
        batch_info['policy_gradient_data'] = []

        rollout_tensors = {k: v for k, v in rollout.items() if isinstance(v, torch.Tensor)}

        for i in range(self.settings.experience_replay):
            # Repeat the experience N times
            np.random.shuffle(indices)

            for sub_indices in np.array_split(indices, batch_splits):
                batch_rollout = {k: v[sub_indices] for k, v in rollout_tensors.items()}

                self.policy_gradient.optimizer_step(
                    batch_info=batch_info,
                    device=self.device,
                    model=self.model,
                    rollout=batch_rollout
                )

        batch_info['frames'] = torch.tensor(rollout_size).to(self.device)
        batch_info['episode_infos'] = rollout['episode_information']
        batch_info['advantage_norm'] = torch.norm(rollout['advantages'])
        batch_info['values'] = rollout['values']
        batch_info['rewards'] = rollout['discounted_rewards']

        # Aggregate policy gradient data
        data_dict_keys = {y for x in batch_info['policy_gradient_data'] for y in x.keys()}

        for key in data_dict_keys:
            # Just average all the statistics from the loss function
            batch_info[key] = torch.mean(torch.stack([d[key] for d in batch_info['policy_gradient_data']]))


class PolicyGradientReinforcerFactory(ReinforcerFactory):
    """ Vel factory class for the PolicyGradientReinforcer """
    def __init__(self, settings, env_factory: VecEnvFactory, model_factory: ModelFactory,
                 policy_gradient: PolicyGradientBase, env_roller_factory: EnvRollerFactory,
                 parallel_envs: int, seed: int):

        self.settings = settings

        self.env_factory = env_factory
        self.model_factory = model_factory
        self.policy_gradient = policy_gradient
        self.env_roller_factory = env_roller_factory
        self.parallel_envs = parallel_envs
        self.seed = seed

    def instantiate(self, device: torch.device) -> ReinforcerBase:
        env = self.env_factory.instantiate(parallel_envs=self.parallel_envs, seed=self.seed)
        model = self.model_factory.instantiate(action_space=env.action_space)
        env_roller = self.env_roller_factory.instantiate(environment=env, device=device, settings=self.settings)

        return PolicyGradientReinforcer(device, self.settings, env, model, self.policy_gradient, env_roller)
