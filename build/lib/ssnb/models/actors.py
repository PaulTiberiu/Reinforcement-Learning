from abc import ABC

import torch
import torch.nn as nn
from torch.distributions.normal import Normal
from torch.distributions import Bernoulli, Independent

from bbrl.agents.agent import Agent
from bbrl.utils.distributions import SquashedDiagGaussianDistribution

from ssnb.models.shared_models import build_mlp, build_backbone


class BaseActor(Agent, ABC):
    """Generic class to centralize copy_parameters"""

    def copy_parameters(self, other):
        """Copy parameters from other agent"""
        for self_p, other_p in zip(self.parameters(), other.parameters()):
            self_p.data.copy_(other_p)


class ProbAgent(Agent):
    def __init__(self, state_dim, hidden_layers, n_action):
        super().__init__(name="prob_agent")
        self.model = build_mlp(
            [state_dim] + list(hidden_layers) + [n_action], activation=nn.Tanh()
        )

    def forward(self, t, **kwargs):
        observation = self.get(("env/env_obs", t))
        scores = self.model(observation)
        action_probs = torch.softmax(scores, dim=-1)
        assert not torch.any(torch.isnan(action_probs)), "Nan Here"
        self.set(("action_probs", t), action_probs)
        entropy = torch.distributions.Categorical(action_probs).entropy()
        self.set(("entropy", t), entropy)


class ActionAgent(Agent):
    def __init__(self):
        super().__init__()

    def forward(self, t, stochastic, **kwargs):
        probs = self.get(("action_probs", t))
        if stochastic:
            action = torch.distributions.Categorical(probs).sample()
        else:
            action = probs.argmax(1)

        self.set(("action", t), action)


class DiscreteActor(BaseActor):
    def __init__(self, state_dim, hidden_size, n_actions):
        super().__init__()
        self.model = build_mlp(
            [state_dim] + list(hidden_size) + [n_actions], activation=nn.ReLU()
        )

    def get_distribution(self, obs):
        scores = self.model(obs)
        probs = torch.softmax(scores, dim=-1)
        return torch.distributions.Categorical(probs)

    def forward(
        self, t, stochastic=False, predict_proba=False, compute_entropy=False, **kwargs
    ):
        """
        Compute the action given either a time step (looking into the workspace)
        or an observation (in kwargs)
        """
        if "observation" in kwargs:
            observation = kwargs["observation"]
        else:
            observation = self.get(("env/env_obs", t))
        scores = self.model(observation)
        probs = torch.softmax(scores, dim=-1)
        if predict_proba:
            action = self.get(("action", t))
            log_prob = probs[torch.arange(probs.size()[0]), action].log()
            self.set(("logprob_predict", t), log_prob)
        else:
            if stochastic:
                action = torch.distributions.Categorical(probs).sample()
            else:
                action = scores.argmax(1)

            log_probs = probs[torch.arange(probs.size()[0]), action].log()

            self.set(("action", t), action)
            self.set(("action_logprobs", t), log_probs)

        if compute_entropy:
            entropy = torch.distributions.Categorical(probs).entropy()
            self.set(("entropy", t), entropy)

    def predict_action(self, obs, stochastic=False):
        scores = self.model(obs)

        if stochastic:
            probs = torch.softmax(scores, dim=-1)
            action = torch.distributions.Categorical(probs).sample()
        else:
            action = scores.argmax(0)
        return action


class DiscreteDeterministicActor(BaseActor):
    def __init__(self, state_dim, hidden_size, n_actions):
        super().__init__()
        self.model = build_mlp(
            [state_dim] + list(hidden_size) + [n_actions], activation=nn.ReLU()
        )

    def forward(self, t, **kwargs):
        """
        Compute the action given either a time step (looking into the workspace)
        or an observation (in kwargs)
        """
        if "observation" in kwargs:
            observation = kwargs["observation"]
        else:
            observation = self.get(("env/env_obs", t))
        action = self.model(observation)
        self.set(("action", t), action)

    def predict_action(self, obs):
        action = self.model(obs)
        return action


class BernoulliActor(Agent):
    def __init__(self, state_dim, hidden_layers):
        super().__init__()
        layers = [state_dim] + list(hidden_layers) + [1]
        self.model = build_mlp(
            layers, activation=nn.ReLU(), output_activation=nn.Sigmoid()
        )

    def forward(self, t, stochastic=False, **kwargs):
        obs = self.get(("env/env_obs", t))
        mean = self.model(obs)
        dist = Bernoulli(mean)
        self.set(("entropy", t), dist.entropy())
        if stochastic:
            action = dist.sample().int().squeeze(-1)
        else:
            act = mean.lt(0.5)
            action = act.squeeze(-1)
        # print(f"stoch:{stochastic} obs:{obs} mean:{mean} dist:{dist} action:{action}")
        log_prob = dist.log_prob(action.float()).sum(axis=-1)
        self.set(("action", t), action)
        self.set(("action_logprobs", t), log_prob)

    def predict_action(self, obs, stochastic=False):
        mean = self.model(obs)
        dist = Bernoulli(mean)
        if stochastic:
            act = dist.sample().int()
            return act
        else:
            act = mean.lt(0.5)
        return act


# All the actors below use a Gaussian policy, that is the output is Normal distribution


class TunableVarianceContinuousActor(BaseActor):
    def __init__(self, state_dim, hidden_layers, action_dim):
        super().__init__()
        layers = [state_dim] + list(hidden_layers) + [action_dim]
        self.model = build_mlp(layers, activation=nn.ReLU())
        init_variance = torch.randn(action_dim, 1).transpose(0, 1)
        self.std_param = nn.parameter.Parameter(init_variance)
        self.soft_plus = torch.nn.Softplus()

    def get_distribution(self, obs: torch.Tensor):
        mean = self.model(obs)
        return Independent(Normal(mean, self.soft_plus(self.std_param[:, 0])), 1)

    def forward(
        self, t, stochastic=False, predict_proba=False, compute_entropy=False, **kwargs
    ):
        obs = self.get(("env/env_obs", t))
        if predict_proba:
            action = self.get(("action", t))
            mean = self.model(obs)
            dist = Normal(mean, self.soft_plus(self.std_param))
            log_prob = dist.log_prob(action).sum(axis=-1)
            self.set(("logprob_predict", t), log_prob)
        else:
            mean = self.model(obs)
            dist = Normal(mean, self.soft_plus(self.std_param))  # std must be positive
            if compute_entropy:
                self.set(("entropy", t), dist.entropy())
            if stochastic:
                action = dist.sample()
            else:
                action = mean
            log_prob = dist.log_prob(action).sum(axis=-1)
            self.set(("action", t), action)
            self.set(("action_logprobs", t), log_prob)

    def predict_action(self, obs, stochastic=False):
        """Predict just one action (without using the workspace)"""
        if stochastic:
            mean = self.model(obs)
            dist = Normal(mean, self.soft_plus(self.std_param))
            return dist.sample()
        else:
            return self.model(obs)


class TunableVarianceContinuousActorExp(BaseActor):
    def __init__(self, state_dim, hidden_layers, action_dim):
        super().__init__()
        layers = [state_dim] + list(hidden_layers) + [action_dim]
        self.model = build_mlp(layers, activation=nn.Tanh())
        self.std_param = nn.parameter.Parameter(torch.randn(1, action_dim))

    def get_distribution(self, obs: torch.Tensor):
        mean = self.model(obs)
        std = torch.clamp(self.std_param, -20, 2)
        return Independent(Normal(mean, torch.exp(std)), 1)

    def forward(
        self,
        t,
        *,
        stochastic=True,
        predict_proba=False,
        compute_entropy=False,
        **kwargs,
    ):
        obs = self.get(("env/env_obs", t))
        dist = self.get_distribution(obs)

        if predict_proba:
            action = self.get(("action", t))
            self.set(("logprob_predict", t), dist.log_prob(action))
        else:
            action = dist.sample() if stochastic else dist.mean
            logp_pi = dist.log_prob(action)

            self.set(("action", t), action)
            self.set(("action_logprobs", t), logp_pi)

        if compute_entropy:
            self.set(("entropy", t), dist.entropy())

    def predict_action(self, obs, stochastic):
        """Predict just one action (without using the workspace)"""
        if stochastic:
            mean = self.model(obs)
            dist = Normal(mean, self.soft_plus(self.std_param))
            return dist.sample()
        else:
            return self.model(obs)


class StateDependentVarianceContinuousActor(BaseActor):
    def __init__(self, state_dim, hidden_layers, action_dim):
        super().__init__()
        backbone_dim = [state_dim] + list(hidden_layers)
        self.layers = build_backbone(backbone_dim, activation=nn.Tanh())
        self.backbone = nn.Sequential(*self.layers)

        self.last_mean_layer = nn.Linear(hidden_layers[-1], action_dim)
        self.last_std_layer = nn.Linear(hidden_layers[-1], action_dim)

    def forward(self, t, stochastic=False, **kwargs):
        obs = self.get(("env/env_obs", t))
        backbone_output = self.backbone(obs)
        mean = self.last_mean_layer(backbone_output)
        std_out = self.last_std_layer(backbone_output)
        std = torch.exp(std_out)
        assert not torch.any(torch.isnan(mean)), "Nan Here"
        dist = Normal(mean, std)
        self.set(("entropy", t), dist.entropy())
        # print("entropy", dist.entropy())
        if stochastic:
            action = dist.sample()
        else:
            action = mean
        log_prob = dist.log_prob(action).sum(axis=-1)
        self.set(("action", t), action)
        self.set(("action_logprobs", t), log_prob)

    def predict_action(self, obs, stochastic=False):
        """Predict just one action (without using the workspace)"""
        backbone_output = self.backbone(obs)
        mean = self.last_mean_layer(backbone_output)
        std_out = self.last_std_layer(backbone_output)
        std = torch.exp(std_out)
        assert not torch.any(torch.isnan(mean)), "Nan Here"
        dist = Normal(mean, std)
        if stochastic:
            action = dist.sample()
        else:
            action = mean
        return action


class ConstantVarianceContinuousActor(BaseActor):
    def __init__(self, state_dim, hidden_layers, action_dim):
        super().__init__()
        layers = [state_dim] + list(hidden_layers) + [action_dim]
        self.model = build_mlp(layers, activation=nn.Tanh())
        self.std_param = 2

    def forward(self, t, stochastic=False, **kwargs):
        obs = self.get(("env/env_obs", t))
        mean = self.model(obs)
        dist = Normal(mean, self.std_param)  # std must be positive
        self.set(("entropy", t), dist.entropy())
        if stochastic:
            action = dist.sample()
        else:
            action = mean
        log_prob = dist.log_prob(action).sum(axis=-1)
        self.set(("action", t), action)
        self.set(("action_logprobs", t), log_prob)

    def predict_action(self, obs, stochastic=False):
        """Predict just one action (without using the workspace)"""
        mean = self.model(obs)
        dist = Normal(mean, self.std_param)
        if stochastic:
            action = dist.sample()
        else:
            action = mean
        return action


class ContinuousDeterministicActor(BaseActor):
    def __init__(self, state_dim, hidden_layers, action_dim):
        super().__init__()
        layers = [state_dim] + list(hidden_layers) + [action_dim]
        self.model = build_mlp(
            layers, activation=nn.Tanh(), output_activation=nn.Tanh()
        )

    def forward(self, t, render=None):
        obs = self.get(("env/env_obs", t))
        action = self.model(obs)
        self.set(("action", t), action)

    def predict_action(self, obs, stochastic=False):
        """Predict just one action (without using the workspace)"""
        assert (
            not stochastic
        ), "ContinuousDeterministicActor cannot provide stochastic predictions"
        return self.model(obs)


class ActorAgent(Agent):
    """Choose an action (either according to p(a_t|s_t) when stochastic is true,
    or with argmax if false.
    """

    def __init__(self):
        super().__init__()

    def forward(self, t, stochastic, **kwargs):
        probs = self.get(("action_probs", t))
        if stochastic:
            action = torch.distributions.Categorical(probs).sample()
        else:
            action = probs.argmax(1)

        self.set(("action", t), action)


class SquashedGaussianActor(BaseActor):
    def __init__(self, state_dim, hidden_layers, action_dim):
        super().__init__()
        backbone_dim = [state_dim] + list(hidden_layers)
        self.layers = build_backbone(backbone_dim, activation=nn.Tanh())
        self.backbone = nn.Sequential(*self.layers)
        self.last_mean_layer = nn.Linear(hidden_layers[-1], action_dim)
        self.last_std_layer = nn.Linear(hidden_layers[-1], action_dim)
        self.action_dist = SquashedDiagGaussianDistribution(action_dim)

    def get_distribution(self, obs: torch.Tensor):
        backbone_output = self.backbone(obs)
        mean = self.last_mean_layer(backbone_output)
        std_out = self.last_std_layer(backbone_output)

        std_out = std_out.clamp(-20, 2)  # as in the official code
        std = torch.exp(std_out)
        return self.action_dist.make_distribution(mean, std)

    def forward(self, t, stochastic=False, predict_proba=False, **kwargs):
        action_dist = self.get_distribution(self.get(("env/env_obs", t)))
        if predict_proba:
            action = self.get(("action", t))
            log_prob = action_dist.log_prob(action)
            self.set(("logprob_predict", t), log_prob)
        else:
            if stochastic:
                action = action_dist.sample()
            else:
                action = action_dist.mode()
            log_prob = action_dist.log_prob(action)
            self.set(("action", t), action)
            self.set(("action_logprobs", t), log_prob)

    def predict_action(self, obs, stochastic=False):
        """Predict just one action (without using the workspace)"""
        action_dist = self.get_distribution(obs)
        return action_dist.sample() if stochastic else action_dist.mode()

    def test(self, obs, action):
        action_dist = self.get_distribution(obs)
        return action_dist.log_prob(action)
