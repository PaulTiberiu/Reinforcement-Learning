import sys
import os
import optuna

from omegaconf import OmegaConf

from ssnb.models.loggers import RewardLogger
from ssnb.algos.td3 import run_td3
from ssnb.algos.ddpg import run_ddpg

from optuna.samplers import TPESampler
from optuna.pruners import MedianPruner
from optuna.visualization import plot_optimization_history, plot_param_importances



def sample_td3_params(trial, cfg):
    """Sampler for TD3 hyperparameters."""
    
    params = cfg.copy()
    
    # discount factor between 0.9 and 0.9999
    params.algorithm.discount_factor = trial.suggest_float("discount_factor", cfg.algorithm.discount_factor.min, cfg.algorithm.discount_factor.max, log=True)
    
    # n_steps 128, 256, 512, ...
    n_steps = 2 ** trial.suggest_int("n_steps", cfg.algorithm.n_steps.min, cfg.algorithm.n_steps.max)
    
    # buffer_size between 1e5 and 1e6
    params.algorithm.buffer_size = trial.suggest_int("buffer_size", cfg.algorithm.buffer_size.min, cfg.algorithm.buffer_size.max)
    
    # batch_size between 100 and 300
    params.algorithm.batch_size = trial.suggest_int("batch_size", cfg.algorithm.batch_size.min, cfg.algorithm.batch_size.max)
    
    # tau_target between 0.05 and 0.005
    params.algorithm.tau_target = trial.suggest_float("tau_target", cfg.algorithm.tau_target.min, cfg.algorithm.tau_target.max, log=True)
    
    # action_noise between 0 and 0.1
    params.algorithm.action_noise = trial.suggest_float("action_std", cfg.algorithm.action_noise.min, cfg.algorithm.action_noise.max, log=True)
    
    # actor hidden size between [32, 32] and [256, 256]
    ahs = 2 ** trial.suggest_int("actor_hidden_size", cfg.algorithm.architecture.actor_hidden_size.min, cfg.algorithm.architecture.actor_hidden_size.max)
    params.algorithm.architecture.actor_hidden_size = [ahs, ahs]
    
    # critic hidden size between [32, 32] and [256, 256]
    chs = 2 ** trial.suggest_int("critic_hidden_size", cfg.algorithm.architecture.critic_hidden_size.min, cfg.algorithm.architecture.critic_hidden_size.max)
    params.algorithm.architecture.critic_hidden_size = [chs, chs]
    
    # actor learning rate between 1e-5 and 1
    params.actor_optimizer.lr = trial.suggest_float("actor_lr", cfg.actor_optimizer.lr.min, cfg.actor_optimizer.lr.max, log=True)
    # critic learning rate between 1e-5 and 1
    params.critic_optimizer.lr = trial.suggest_float("critic_lr", cfg.critic_optimizer.lr.min, cfg.critic_optimizer.lr.max, log=True)

    params.algorithm.n_steps = n_steps
    params.algorithm.max_epochs = int(params.algorithm.n_timesteps // n_steps) # to have a run of n_timesteps
    
    return params

def objective(trial, cfg, run_algo):
    config = sample_td3_params(trial, cfg)
    nan_encountered = False
    
    try:
        # Train the model
        agent = None
        max = config.algorithm.max_epoch
        config.algorithm.max_epoch = 1
        
        for epoch in range(config.algorithm.max_epoch):
            eval(run_algo + "(config, agent)")

            if agent.is_eval:
                trial.report(agent.mean, epoch)

                if trial.should_prune():
                    raise optuna.exceptions.TrialPruned()
                
        config.algorithm.max_epoch = max

    except AssertionError as e:
        # Sometimes, random hyperparams can generate NaN
        print(e)
        nan_encountered = True

    # Tell the optimizer that the trial failed
    if nan_encountered:
        return float("nan")
    
    return agent.last_mean_reward

def tune(objective, cfg, run_algo):
	# Creation and start of the study
	sampler = TPESampler(n_startup_trials=cfg.study.n_startup_trials)
	pruner = MedianPruner(n_startup_trials=cfg.study.n_startup_trials, n_warmup_steps=cfg.study.n_warmup_steps // 3)
	study = optuna.create_study(sampler=sampler, pruner=pruner, direction="maximize")
	
	try:
		study.optimize(lambda trial: objective(trial, cfg, run_algo), n_trials=cfg.study.n_trials, n_jobs=cfg.study.n_jobs, timeout=cfg.study.timeout)
	except KeyboardInterrupt:
		pass
	
	print("Number of finished trials: ", len(study.trials))
	
	# Best trial
	print("Best trial:")
	trial = study.best_trial
	print(f"  Value: {trial.value}")
	print("  Params: ")
	for key, value in trial.params.items():
		print(f"    {key}: {value}")
	print("  User attrs:")
	for key, value in trial.user_attrs.items():
		print(f"    {key}: {value}")

	# Write report
	study.trials_dataframe().to_csv("study_results_td3_swimmer.csv")

	fig1 = plot_optimization_history(study)
	fig2 = plot_param_importances(study)

	fig1.show()
	fig2.show()

def main(argv):
    cfg = OmegaConf.load("./configs/" + argv[0])
    tune(objective, cfg, argv[1])

if __name__ == "__main__":
    sys.path.append(os.getcwd())
    main(sys.argv[1:])