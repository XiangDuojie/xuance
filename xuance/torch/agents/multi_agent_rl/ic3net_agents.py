import numpy as np
import torch
from argparse import Namespace
from operator import itemgetter
from xuance.environment import DummyVecMultiAgentEnv, SubprocVecMultiAgentEnv
from xuance.common import List, Optional, Union, I3CNet_Buffer, I3CNet_Buffer_RNN
from xuance.torch import Module
from xuance.torch.utils import NormalizeFunctions, ActivationFunctions
from xuance.torch.communications import IC3NetComm
from xuance.torch.policies import REGISTRY_Policy
from xuance.torch.agents.multi_agent_rl.iac_agents import IAC_Agents


class IC3Net_Agents(IAC_Agents):
    """The implementation of IC3Net_Agents agents.

    Args:
        config: the Namespace variable that provides hyper-parameters and other settings.
        envs: the vectorized environments.
    """

    def __init__(self,
                 config: Namespace,
                 envs: Union[DummyVecMultiAgentEnv, SubprocVecMultiAgentEnv]):
        super(IC3Net_Agents, self).__init__(config, envs)
        self.policy = self._build_policy()  # build policy
        self.memory = self._build_memory()  # build memory
        self.learner = self._build_learner(self.config, self.model_keys, self.agent_keys, self.policy)

    def _build_memory(self):
        """Build replay buffer for models training
        """
        if self.use_actions_mask:
            avail_actions_shape = {key: (self.action_space[key].n,) for key in self.agent_keys}
        else:
            avail_actions_shape = None
        input_buffer = dict(agent_keys=self.agent_keys,
                            state_space=self.state_space if self.use_global_state else None,
                            obs_space=self.observation_space,
                            act_space=self.action_space,
                            n_envs=self.n_envs,
                            buffer_size=self.config.buffer_size,
                            use_gae=self.config.use_gae,
                            use_advnorm=self.config.use_advnorm,
                            gamma=self.config.gamma,
                            gae_lam=self.config.gae_lambda,
                            avail_actions_shape=avail_actions_shape,
                            use_actions_mask=self.use_actions_mask,
                            max_episode_steps=self.episode_length,
                            dim_message=self.config.dim_message)
        Buffer = I3CNet_Buffer_RNN if self.use_rnn else I3CNet_Buffer
        return Buffer(**input_buffer)

    def _build_policy(self) -> Module:
        """
        Build representation(s) and policy(ies) for agent(s)

        Returns:
            policy (torch.nn.Module): A dict of policies.
        """
        normalize_fn = NormalizeFunctions[self.config.normalize] if hasattr(self.config, "normalize") else None
        initializer = torch.nn.init.orthogonal_
        activation = ActivationFunctions[self.config.activation]
        device = self.device
        agent = self.config.agent

        # build representations
        rep_input_space = {k: (self.config.obs_encode_dim, ) for k in self.agent_keys}
        representation = self._build_representation(self.config.representation, rep_input_space, self.config)

        # build communicators
        communicators = IC3NetComm(self.observation_space, self.action_space, self.config.obs_encode_dim, self.n_agents,
                                   self.config.communicator_hidden_size, self.config.n_action_heads,
                                   self.config.comm_mask_zero, self.config.comm_passes,
                                   init_std=0.2, recurrent=self.config.use_rnn, comm_init=self.config.comm_init,
                                   device=self.device, use_distributed_training=self.distributed_training,
                                   use_parameter_sharing=self.use_parameter_sharing, model_keys=self.model_keys,
                                   continuous=self.config.continuous)
        # build policies
        if self.config.policy == "IC3NetPolicy":
            policy = REGISTRY_Policy["IC3NetPolicy"](
                action_space=self.action_space, n_agents=self.n_agents,
                representation=representation, communicators=communicators,
                actor_hidden_size=self.config.actor_hidden_size, critic_hidden_size=self.config.critic_hidden_size,
                normalize=normalize_fn, initialize=initializer, activation=activation,
                device=device, use_distributed_training=self.distributed_training,
                use_parameter_sharing=self.use_parameter_sharing, model_keys=self.model_keys,
                use_rnn=self.use_rnn, rnn=self.config.rnn if self.use_rnn else None)
        else:
            raise AttributeError(f"{agent} currently does not support the policy named {self.config.policy}.")
        return policy

    def init_rnn_hidden(self, n_envs):
        """
        Returns initialized hidden states of RNN if use RNN-based representations.

        Parameters:
            n_envs (int): The number of parallel environments.
        """
        rnn_hidden = None
        if self.use_rnn:
            batch = n_envs * self.n_agents if self.use_parameter_sharing else n_envs
            rnn_hidden = {k: self.policy.representation[k].init_hidden(batch) for k in self.model_keys}
        return rnn_hidden, None

    def init_hidden_item(self,
                         i_env: int,
                         rnn_hidden_actor: Optional[dict] = None,
                         rnn_hidden_critic: Optional[dict] = None):
        """
        Returns initialized hidden states of RNN for i-th environment.

        Parameters:
            i_env (int): The index of environment that to be selected.
            rnn_hidden_actor (Optional[dict]): The RNN hidden states of actor representation.
            rnn_hidden_critic (Optional[dict]): The RNN hidden states of critic representation.
        """
        assert self.use_rnn is True, "This method cannot be called when self.use_rnn is False."
        if self.use_parameter_sharing:
            b_index = np.arange(i_env * self.n_agents, (i_env + 1) * self.n_agents)
        else:
            b_index = [i_env, ]
        for k in self.model_keys:
            rnn_hidden_actor[k] = self.policy.actor_representation[k].init_hidden_item(b_index, *rnn_hidden_actor[k])
        if rnn_hidden_critic is None:
            return rnn_hidden_actor, None
        for k in self.model_keys:
            rnn_hidden_critic[k] = self.policy.critic_representation[k].init_hidden_item(b_index, *rnn_hidden_critic[k])
        return rnn_hidden_actor, rnn_hidden_critic

    def store_experience(self, obs_dict, avail_actions, actions_dict, log_pi_a, rewards_dict, values_dict,
                         terminals_dict, info, **kwargs):
        """
        Store experience data into replay buffer.

        Parameters:
            obs_dict (List[dict]): Observations for each agent in self.agent_keys.
            avail_actions (List[dict]): Actions mask values for each agent in self.agent_keys.
            actions_dict (List[dict]): Actions for each agent in self.agent_keys.
            log_pi_a (dict): The log of pi.
            rewards_dict (List[dict]): Rewards for each agent in self.agent_keys.
            values_dict (dict): Critic values for each agent in self.agent_keys.
            terminals_dict (List[dict]): Terminated values for each agent in self.agent_keys.
            info (List[dict]): Other information for the environment at current step.
            **kwargs: Other inputs.
        """
        experience_data = {
            'obs': {k: np.array([data[k] for data in obs_dict]) for k in self.agent_keys},
            'actions': {k: np.array([data[k] for data in actions_dict]) for k in self.agent_keys},
            # 'log_pi_old': log_pi_a,
            'rewards': {k: np.array([np.array(list(data.values())).mean() for data in rewards_dict])
                        for k in self.agent_keys},
            'values': values_dict,
            'terminals': {k: np.array([data[k] for data in terminals_dict]) for k in self.agent_keys},
            'agent_mask': {k: np.array([data['agent_mask'][k] for data in info]) for k in self.agent_keys},
        }
        if self.use_rnn:
            experience_data['episode_steps'] = np.array([data['episode_step'] - 1 for data in info])
        if self.use_global_state:
            experience_data['state'] = np.array(kwargs['state'])
        if self.use_actions_mask:
            experience_data['avail_actions'] = {k: np.array([data[k] for data in avail_actions])
                                                for k in self.agent_keys}
        self.memory.store(**experience_data)

    def action(self,
               obs_dict: List[dict],
               state: Optional[np.ndarray] = None,
               avail_actions_dict: Optional[List[dict]] = None,
               rnn_hidden_actor: Optional[dict] = None,
               rnn_hidden_critic: Optional[dict] = None,
               test_mode: Optional[bool] = False,
               **kwargs):
        """
        Returns actions for agents.

        Parameters:
            obs_dict (dict): Observations for each agent in self.agent_keys.
            state (Optional[np.ndarray]): The global state.
            avail_actions_dict (Optional[List[dict]]): Actions mask values, default is None.
            rnn_hidden_actor (Optional[dict]): The RNN hidden states of actor representation.
            rnn_hidden_critic (Optional[dict]): The RNN hidden states of critic representation.
            test_mode (Optional[bool]): True for testing without noises.

        Returns:
            rnn_hidden_actor_new (dict): The new RNN hidden states of actor representation (if self.use_rnn=True).
            rnn_hidden_critic_new (dict): The new RNN hidden states of critic representation (if self.use_rnn=True).
            actions_dict (dict): The output actions.
            log_pi_a (dict): The log of pi.
            values_dict (dict): The evaluated critic values (when test_mode is False).
        """
        n_env = len(obs_dict)
        rnn_hidden_critic_new, log_pi_a_dict, values_dict = {}, {}, {}

        obs_input, agents_id, avail_actions_input = self._build_inputs(obs_dict, avail_actions_dict)
        rnn_hidden_actor_new, pi_dists = self.policy(observation=obs_input,
                                                     agent_ids=agents_id,
                                                     avail_actions=avail_actions_input,
                                                     rnn_hidden=rnn_hidden_actor)
        if not test_mode:
            rnn_hidden_critic_new, values_dict = self.policy.get_values(observation=obs_input,
                                                                        agent_ids=agents_id,
                                                                        rnn_hidden=rnn_hidden_critic)
            if self.use_parameter_sharing:
                values_dict = {k: values_dict[self.model_keys[0]].detach().cpu().numpy().reshape(n_env, -1)[:, i]
                               for i, k in enumerate(self.agent_keys)}
            else:
                values_dict = {k: v.detach().cpu().numpy().reshape(n_env) for k, v in values_dict.items()}

        if self.use_parameter_sharing:
            key = self.agent_keys[0]
            actions_sample = pi_dists[key].stochastic_sample()
            if self.continuous_control:
                actions_out = actions_sample.reshape(n_env, self.n_agents, -1)
            else:
                actions_out = actions_sample.reshape(n_env, self.n_agents)
            actions_dict = [{k: actions_out[e, i].cpu().detach().numpy() for i, k in enumerate(self.agent_keys)}
                            for e in range(n_env)]
        else:
            actions_sample = {k: pi_dists[k].stochastic_sample() for k in self.agent_keys}
            if self.continuous_control:
                actions_dict = [{k: actions_sample[k].cpu().detach().numpy()[e].reshape([-1]) for k in self.agent_keys}
                                for e in range(n_env)]
            else:
                actions_dict = [{k: actions_sample[k].cpu().detach().numpy()[e].reshape([]) for k in self.agent_keys}
                                for e in range(n_env)]

        return {"rnn_hidden_actor": rnn_hidden_actor_new, "rnn_hidden_critic": rnn_hidden_critic_new,
                "actions": actions_dict, "log_pi": None, "values": values_dict}

    def values_next(self,
                    i_env: int,
                    obs_dict: dict,
                    state: Optional[np.ndarray] = None,
                    rnn_hidden_critic: Optional[dict] = None):
        """
        Returns critic values of one environment that finished an episode.

        Parameters:
            i_env (int): The index of environment.
            obs_dict (dict): Observations for each agent in self.agent_keys.
            state (Optional[np.ndarray]): The global state.
            rnn_hidden_critic (Optional[dict]): The RNN hidden states of critic representation.

        Returns:
            rnn_hidden_critic_new (dict): The new RNN hidden states of critic representation (if self.use_rnn=True).
            values_dict: The critic values.
        """
        n_env = 1
        rnn_hidden_critic_i = None
        if self.use_parameter_sharing:
            key = self.agent_keys[0]
            if self.use_rnn:
                hidden_item_index = np.arange(i_env * self.n_agents, (i_env + 1) * self.n_agents)
                rnn_hidden_critic_i = {key: self.policy.critic_representation[key].get_hidden_item(
                    hidden_item_index, *rnn_hidden_critic[key])}
                batch_size = n_env * self.n_agents
                obs_array = np.array(itemgetter(*self.agent_keys)(obs_dict))
                obs_input = {key: obs_array.reshape([batch_size, 1, -1])}
                agents_id = torch.eye(self.n_agents).unsqueeze(0).expand(n_env, -1, -1).reshape(batch_size, 1, -1).to(
                    self.device)
            else:
                obs_input = {key: np.array([itemgetter(*self.agent_keys)(obs_dict)])}
                agents_id = torch.eye(self.n_agents).unsqueeze(0).expand(n_env, -1, -1).to(self.device)

            rnn_hidden_critic_new, values_dict = self.policy.get_values(observation=obs_input,
                                                                        agent_ids=agents_id,
                                                                        rnn_hidden=rnn_hidden_critic_i)
            values_out = values_dict[key].detach().cpu().numpy().reshape(-1)
            values_dict = {k: values_out[i] for i, k in enumerate(self.agent_keys)}
        else:
            if self.use_rnn:
                rnn_hidden_critic_i = {k: self.policy.critic_representation[k].get_hidden_item(
                    [i_env, ], *rnn_hidden_critic[k]) for k in self.agent_keys}
            obs_input = {k: obs_dict[k][None, None, :] for k in self.agent_keys} if self.use_rnn else obs_dict
            rnn_hidden_critic_new, values_dict = self.policy.get_values(observation=obs_input,
                                                                        rnn_hidden=rnn_hidden_critic_i)
            values_dict = {k: v.detach().cpu().numpy().reshape([]) for k, v in values_dict.items()}
        return rnn_hidden_critic_new, values_dict

