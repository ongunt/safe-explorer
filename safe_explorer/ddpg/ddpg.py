import copy
import numpy as np
import time
import torch
from torch.optim import Adam
from torch.utils.tensorboard import SummaryWriter

from safe_explorer.core.config import Config
from safe_explorer.ddpg.replay_buffer import ReplayBuffer

class DDPG:
    def __init__(self, env, actor, critic):
        self._env = env
        self._actor = actor
        self._critic = critic

        self._config = Config.get().ddpg.trainer

        self._initialize_target_networks()
        self._initialize_optimizers()

        self._models = [self._actor, self._critic,
                        self._target_actor, self._target_critic]

        self._replay_buffer = ReplayBuffer(self._config.replay_buffer_size)

        # Tensorboard writer
        self._writer = SummaryWriter(self._config.tensorboard_dir)

        if self._config.use_gpu:
            self.cuda()

    def _initialize_target_networks(self):
        self._target_actor = copy.deepcopy(self._actor)
        self._target_critic = copy.deepcopy(self._critic)
    
    def _initialize_optimizers(self):
        self._actor_optimizer = Adam(self._actor.parameters(), lr=self._config.actor_lr)
        self._critic_optimizer = Adam(self._critic.parameters(), lr=self._config.critic_lr)
    
    def eval_mode(self):
        map(lambda x: x.eval(), self._models)

    def train_mode(self):
        map(lambda x: x.train(), self._models)

    def cuda(self):
        map(lambda x: x.eval(), self._models)

    def _get_action(self, observation, is_training=True):
        # Action + random gaussian noise (as recommended in spining up)
        action = self._actor(self._tuple_to_tensor(observation))
        if is_training:
            action += self._config.action_noise_range * torch.randn(observation.shape[0])
        action = np.clip(action.data.numpy(),
                         self._env.action_space.low,
                         self._env.action_space.hight)
        return action

    def _get_q(self, batch):
        return self._critic(torch.Tensor(batch["observation"]))

    def _get_target(self, batch):
        # For each observation in batch:
        # target = r + discount_factor * (1 - done) * max_a Q_tar(s, a)
        # a => actions of actor on current observations
        # max_a Q_tar(s, a) = output of critic
        observation = torch.Tensor(batch["observation"])
        reward = torch.Tensor(batch["reward"])
        done = torch.Tensor(batch["done"])

        action = self._target_actor(observation)
        q = self._target_critic(observation, action)

        return reward  + self._config.discount_factor * (1 - done) + q

    def _tuple_to_tensor(self, tup):
        return torch.Tensor(np.concatenate(list(tup)))

    def _update_targets(self, target, main):
        for target_param, main_param in zip(target.parameters(), main.parameters()):
            target_param.data.copy_(self._config.polyak * target_param.data + \
                                    (1 - self._config.polyak) * main_param.data)

    def _update_batch(self):
        batch = self.replay_buffer.sample(self._config.batch_size)

        q_predicted = self._critic(torch.Tensor(batch["observation"]),
                                    torch.Tensor(batch["action"]))

        q_target = self._get_target(batch)
        
        # Update critic
        self._critic_optimizer.zero_grad()
        critic_loss = -torch.mean((q_predicted - q_target) ** 2)
        critic_loss.backward()
        self._critic_optimizer.step()

        # Update actor
        self._actor_optimizer.zero_grad()
        # Find loss with updated critic
        actor_loss = -torch.mean(self._critic(torch.Tensor(batch["observation"]),
                                              self._actor(self._tuple_to_tensor(
                                                          torch.Tensor(batch["observations"])))))
        actor_loss.backward()
        self._actor_optimizer.step()
        
        # Log to tensorboard
        self._writer.add_scalar("critic loss", critic_loss.item())
        self._writer.add_scalar("actor loss", actor_loss.item())
        self._writer.add_scalar("critic loss grad", critic_loss.grad.item())
        self._writer.add_scalar("actor loss grad", actor_loss.grad.item())
        
        # Update targets networks
        self._update_targets(self.target_actor, self._actor)
        self._update_targets(self.target_critic, self._critic)

    def _update(self, episode_length):
        # Update model #episode_length times
        map(lambda x: self._update_batch(), range(episode_length))

    def evaluate(self):
        rewards = []
        lengths = []

        observation = self._env.reset()
        episode_reward = 0
        episode_length = 0

        self.eval_mode()

        for step in range(self._config.evaluation_steps):
            action = self._get_action(observation, is_training=False)
            observation, reward, done, _ = self._env.step(action)
            episode_reward += reward
            episode_length += 1
            
            if done or (episode_length == self._config.max_episode_length):
                rewards.append(episode_reward)
                lengths.append(episode_length)

        self._writer.add_scalar("eval episode length", np.mean(episode_length))
        self._writer.add_scalar("eval episode reward", np.mean(episode_reward))

        self.train_mode()

        print(f"Validation completed with average episode length: {np.mean(episode_length)} \
                & average reward {np.mean(episode_reward)}")

    def train(self):
        
        start_time = time.time()

        print("==========================================================")
        print("Initializing training with config:")
        Config.get().pprint()
        print("==========================================================")
        print(f"Start time: {start_time}")
        print("==========================================================")

        observation = self.env.reset()
        episode_reward = 0
        episode_length = 0

        number_of_steps = self._config.steps_per_epoch * self._config.epochs

        for step in range(number_of_steps):
            # Randomly sample actions for some initial steps
            action = self.env.action_space.sample() if step < self._config.start_steps \
                     else self._get_action(observation)
            
            observation_next, reward, done, _ = self._env.step(action)
            episode_reward += reward
            episode_length += 1

            self.replay_buffer.add({
                "observation": observation,
                "action": action,
                "reward": reward,
                "observation_next": observation_next,
                "done": done,
            })

            observation = observation_next
        
            # Make all updates at the end of the episode
            if done or (episode_length == self._config.max_episode_length):
                self._update(episode_length)
                # Reset episode
                observation = self.env.reset()
                episode_reward = 0
                episode_length = 0
                self._writer.add_scalar("episode length", episode_length)
                self._writer.add_scalar("episode reward", episode_reward)
            
            # Check if the epoch is over
            if step != 0 and step % self._config.steps_per_epoch == 0:
                # self.store()
                print(f"Finished epoch {step & self._config.steps_per_epoch}. Running validation ...")
                self.evaluate()
                print("----------------------------------------------------")
        
        print("==========================================================")
        print(f"Finished training. Time spent: {time.time() - start_time}")
        print("==========================================================")