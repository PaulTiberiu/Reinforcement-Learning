import copy
import gym
import hydra
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import os
import sys
import torch
import torch.nn as nn

from omegaconf import DictConfig

from bbrl import get_arguments, get_class
from bbrl.agents import Agents, TemporalAgent
from bbrl.agents.gymb import AutoResetGymAgent, NoAutoResetGymAgent
from bbrl.utils.chrono import Chrono
from bbrl.utils.replay_buffer import ReplayBuffer
from bbrl.workspace import Workspace

from ssnb.models.actors import ContinuousDeterministicActor
from ssnb.models.critics import ContinuousQAgent
from ssnb.models.exploration_agents import AddGaussianNoise
from ssnb.models.loggers import Logger, RewardLogger
from ssnb.plotters import Plotter

# HYDRA_FULL_ERROR = 1


matplotlib.use("TkAgg")
assets_path = os.getcwd() + "/../assets/"


def soft_update_params(net, target_net, tau):
    for param, target_param in zip(net.parameters(), target_net.parameters()):
        target_param.data.copy_(tau * param.data + (1 - tau) * target_param.data)


# Create the DDPG Agent
def create_ddpg_agent(cfg, train_env_agent, eval_env_agent):
    obs_size, act_size = train_env_agent.get_obs_and_actions_sizes()
    critic = ContinuousQAgent(
        obs_size, cfg.algorithm.architecture.critic_hidden_size, act_size
    )
    target_critic = copy.deepcopy(critic)
    actor = ContinuousDeterministicActor(
        obs_size, cfg.algorithm.architecture.actor_hidden_size, act_size
    )

    # target_actor = copy.deepcopy(actor)
    noise_agent = AddGaussianNoise(cfg.algorithm.action_noise)
    tr_agent = Agents(train_env_agent, actor, noise_agent)  # TODO : add OU noise
    ev_agent = Agents(eval_env_agent, actor)

    # Get an agent that is executed on a complete workspace
    train_agent = TemporalAgent(tr_agent)
    eval_agent = TemporalAgent(ev_agent)
    train_agent.seed(cfg.algorithm.seed)
    
    return train_agent, eval_agent, actor, critic, target_critic  # , target_actor


def make_gym_env(env_name, xml_file):
    xml_file = assets_path + xml_file
    return gym.make(env_name, xml_file)


# Configure the optimizer
def setup_optimizers(cfg, actor, critic):
    actor_optimizer_args = get_arguments(cfg.actor_optimizer)
    parameters = actor.parameters()
    actor_optimizer = get_class(cfg.actor_optimizer)(parameters, **actor_optimizer_args)
    critic_optimizer_args = get_arguments(cfg.critic_optimizer)
    parameters = critic.parameters()
    critic_optimizer = get_class(cfg.critic_optimizer)(
        parameters, **critic_optimizer_args
    )
    return actor_optimizer, critic_optimizer


def compute_critic_loss(cfg, reward, must_bootstrap, q_values, target_q_values):
    # Compute temporal difference
    q_next = target_q_values
    target = (
        reward[:-1].squeeze()
        + cfg.algorithm.discount_factor * q_next.squeeze(-1) * must_bootstrap.int()
    )
    mse = nn.MSELoss()
    critic_loss = mse(target, q_values.squeeze(-1))
    return critic_loss


def compute_actor_loss(q_values):
    return -q_values.mean()


def run_ddpg(cfg, reward_logger):
    # Build the logger
    logger = Logger(cfg)
    best_reward = -10e9
    delta_list = []

    # Create the environment agent
    train_env_agent = AutoResetGymAgent(
        get_class(cfg.gym_env),
        get_arguments(cfg.gym_env),
        cfg.algorithm.n_envs,
        cfg.algorithm.seed,
    )
    eval_env_agent = NoAutoResetGymAgent(
        get_class(cfg.gym_env),
        get_arguments(cfg.gym_env),
        cfg.algorithm.nb_evals,
        cfg.algorithm.seed,
    )

    # Create the DDPG Agent
    (
        train_agent,
        eval_agent,
        actor,
        critic,
        # target_actor,
        target_critic,
    ) = create_ddpg_agent(cfg, train_env_agent, eval_env_agent)
    ag_actor = TemporalAgent(actor)
    # ag_target_actor = TemporalAgent(target_actor)
    q_agent = TemporalAgent(critic)
    target_q_agent = TemporalAgent(target_critic)

    train_workspace = Workspace()
    rb = ReplayBuffer(max_size=cfg.algorithm.buffer_size)

    # Configure the optimizer
    actor_optimizer, critic_optimizer = setup_optimizers(cfg, actor, critic)
    nb_steps = 0
    tmp_steps = 0

    # Training loop
    for epoch in range(cfg.algorithm.max_epochs):
        # Execute the agent in the workspace
        if epoch > 0:
            train_workspace.zero_grad()
            train_workspace.copy_n_last_steps(1)
            train_agent(train_workspace, t=1, n_steps=cfg.algorithm.n_steps - 1)
        else:
            train_agent(train_workspace, t=0, n_steps=cfg.algorithm.n_steps)

        transition_workspace = train_workspace.get_transitions()
        action = transition_workspace["action"]
        nb_steps += action[0].shape[0]
        rb.put(transition_workspace)

        for _ in range(cfg.algorithm.n_updates):
            rb_workspace = rb.get_shuffled(cfg.algorithm.batch_size)

            done, truncated, reward, action = rb_workspace[
                "env/done", "env/truncated", "env/reward", "action"
            ]
            if nb_steps > cfg.algorithm.learning_starts:
                """
                Determines whether values of the critic should be propagated
                True if the episode reached a time limit or if the task was not done
                See https://colab.research.google.com/drive/1erLbRKvdkdDy0Zn1X_JhC01s1QAt4BBj?usp=sharing
                """
                must_bootstrap = torch.logical_or(~done[1], truncated[1])

                """
                Critic update
                compute q_values: at t, we have Q(s,a) from the (s,a) in the RB
                the detach_actions=True changes nothing in the results
                """
                q_agent(rb_workspace, t=0, n_steps=1, detach_actions=True)
                q_values = rb_workspace["q_value"]

                with torch.no_grad():
                    # replace the action at t+1 in the RB with \pi(s_{t+1}), to compute Q(s_{t+1}, \pi(s_{t+1}) below
                    ag_actor(rb_workspace, t=1, n_steps=1)

                    # compute q_values: at t+1 we have Q(s_{t+1}, \pi(s_{t+1})
                    target_q_agent(rb_workspace, t=1, n_steps=1, detach_actions=True)
                    # q_agent(rb_workspace, t=1, n_steps=1)

                # finally q_values contains the above collection at t=0 and t=1
                post_q_values = rb_workspace["q_value"]

                # Compute critic loss
                critic_loss = compute_critic_loss(
                    cfg, reward, must_bootstrap, q_values[0], post_q_values[1]
                )
                logger.add_log("critic_loss", critic_loss, nb_steps)
                critic_optimizer.zero_grad()
                critic_loss.backward()
                torch.nn.utils.clip_grad_norm_(
                    critic.parameters(), cfg.algorithm.max_grad_norm
                )
                critic_optimizer.step()

                # Actor update : now we determine the actions the current policy would take in the states from the RB
                ag_actor(rb_workspace, t=0, n_steps=1)

                # We determine the Q values resulting from actions of the current policy
                q_agent(rb_workspace, t=0, n_steps=1)

                # And we back-propagate the corresponding loss to maximize the Q values
                q_values = rb_workspace["q_value"]
                actor_loss = compute_actor_loss(q_values)
                logger.add_log("actor_loss", actor_loss, nb_steps)

                # if -25 < actor_loss < 0 and nb_steps > 2e5:
                actor_optimizer.zero_grad()
                actor_loss.backward()
                torch.nn.utils.clip_grad_norm_(
                    actor.parameters(), cfg.algorithm.max_grad_norm
                )
                actor_optimizer.step()

                # Soft update of target q function
                tau = cfg.algorithm.tau_target
                soft_update_params(critic, target_critic, tau)
                # soft_update_params(actor, target_actor, tau)

        if nb_steps - tmp_steps > cfg.algorithm.eval_interval:
            tmp_steps = nb_steps
            eval_workspace = Workspace()  # Used for evaluation
            eval_agent(eval_workspace, t=0, stop_variable="env/done", render=cfg.render_agents) # Used for render

            rewards = eval_workspace["env/cumulated_reward"]
            q_agent(eval_workspace, t=0, stop_variable="env/done")
            q_values = eval_workspace["q_value"].squeeze()
            delta = q_values - rewards
            maxi_delta = delta.max(axis=0)[0].detach().numpy()
            delta_list.append(maxi_delta)

            mean = rewards[-1].mean()
            logger.add_log("reward", mean, nb_steps)
            print(f"nb_steps: {nb_steps}, reward: {mean}")
            reward_logger.add(nb_steps, mean)

            if cfg.save_best and mean > best_reward:
                best_reward = mean
                directory = "./ddpg_agent/"
                if not os.path.exists(directory):
                    os.makedirs(directory)
                filename = (
                    directory
                    + cfg.gym_env.env_name
                    + "#ddpg#T1_T2#"
                    + str(mean.item())
                    + ".agt"
                )
                eval_agent.save_model(filename)

    delta_list_mean = np.array(delta_list).mean(axis=1)
    delta_list_std = np.array(delta_list).std(axis=1)
    return delta_list_mean, delta_list_std


@hydra.main(
    config_path="./configs/ddpg/",
    config_name="ddpg_swimmer5.yaml",
)


def main(cfg: DictConfig):
    chrono = Chrono()
    logdir = "./plot/"
    reward_logger = RewardLogger(logdir + "ddpg.steps", logdir + "ddpg.rwd")
    torch.manual_seed(cfg.algorithm.seed)
    run_ddpg(cfg, reward_logger)
    chrono.stop()


if __name__ == "__main__":
    sys.path.append(os.getcwd())
    main()