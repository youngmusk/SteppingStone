import gym
import torch
import torch.nn as nn
import torch.nn.functional as F


FixedNormal = torch.distributions.Normal

log_prob_normal = FixedNormal.log_prob
FixedNormal.log_probs = lambda self, actions: log_prob_normal(self, actions).sum(
    -1, keepdim=True
)

normal_entropy = FixedNormal.entropy
FixedNormal.entropy = lambda self: normal_entropy(self).sum(-1)

FixedNormal.mode = lambda self: self.mean


class DiagGaussian(nn.Module):
    def __init__(self, num_outputs):
        super(DiagGaussian, self).__init__()
        self.logstd = AddBias(torch.zeros(num_outputs))

    def forward(self, action_mean):
        #  An ugly hack for my KFAC implementation.
        zeros = torch.zeros(action_mean.size())
        if action_mean.is_cuda:
            zeros = zeros.cuda()

        action_logstd = self.logstd(zeros)
        return FixedNormal(action_mean, action_logstd.exp())


class AddBias(nn.Module):
    def __init__(self, bias):
        super(AddBias, self).__init__()
        self._bias = nn.Parameter(bias.unsqueeze(1))

    def forward(self, x):
        if x.dim() == 2:
            bias = self._bias.t().view(1, -1)
        else:
            bias = self._bias.t().view(1, -1, 1, 1)

        return x + bias


def init(module, weight_init, bias_init, gain=1):
    weight_init(module.weight.data, gain=gain)
    bias_init(module.bias.data)
    return module


class Policy(nn.Module):
    def __init__(self, controller):
        super(Policy, self).__init__()
        self.actor = controller
        self.dist = DiagGaussian(controller.action_dim)

        init_ = lambda m: init(
            m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain("relu"),
        )

        state_dim = controller.state_dim
        h_size = 256
        self.critic = nn.Sequential(
            init_(nn.Linear(state_dim, h_size)),
            nn.ReLU(),
            init_(nn.Linear(h_size, h_size)),
            nn.ReLU(),
            init_(nn.Linear(h_size, h_size)),
            nn.ReLU(),
            init_(nn.Linear(h_size, h_size)),
            nn.ReLU(),
            init_(nn.Linear(h_size, 1)),
        )
        self.state_size = 1

    def forward(self, inputs, states, masks):
        raise NotImplementedError

    def act(self, inputs, states, masks, deterministic=False):
        action = self.actor(inputs)
        dist = self.dist(action)

        if deterministic:
            action = dist.mode()
        else:
            action = dist.sample()

        # action.clamp_(-1.0, 1.0)
        action_log_probs = dist.log_probs(action)

        value = self.critic(inputs)

        return value, action, action_log_probs, states

    def get_value(self, inputs, states, masks):
        value = self.critic(inputs)
        return value

    def evaluate_actions(self, inputs, states, masks, action):
        value = self.critic(inputs)
        mode = self.actor(inputs)
        dist = self.dist(mode)

        action_log_probs = dist.log_probs(action)
        dist_entropy = dist.entropy().mean()

        return value, action_log_probs, dist_entropy, states


class SoftsignPolicy(Policy):
    def __init__(self, controller):
        super(SoftsignPolicy, self).__init__(controller)

        init_s_ = lambda m: init(
            m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain("sigmoid"),
        )
        init_r_ = lambda m: init(
            m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain("relu"),
        )

        state_dim = controller.state_dim
        h_size = 256
        self.critic = nn.Sequential(
            init_s_(nn.Linear(state_dim, h_size)),
            nn.Softsign(),
            init_s_(nn.Linear(h_size, h_size)),
            nn.Softsign(),
            init_s_(nn.Linear(h_size, h_size)),
            nn.Softsign(),
            init_r_(nn.Linear(h_size, h_size)),
            nn.ReLU(),
            init_r_(nn.Linear(h_size, h_size)),
            nn.ReLU(),
            init_s_(nn.Linear(h_size, 1)),
        )


class ReluActor(nn.Module):
    """ Simple neural net actor that takes observation as input and outputs torques """

    def __init__(self, env):
        super(ReluActor, self).__init__()
        self.state_dim = env.observation_space.shape[0]
        self.action_dim = env.action_space.shape[0]

        init_ = lambda m: init(
            m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain("relu"),
        )

        h_size = 256
        self.fc1 = init_(nn.Linear(self.state_dim, h_size))
        self.fc2 = init_(nn.Linear(h_size, h_size))
        self.fc3 = init_(nn.Linear(h_size, h_size))
        self.fc4 = init_(nn.Linear(h_size, h_size))
        self.fc5 = init_(nn.Linear(h_size, h_size))
        self.out = init_(nn.Linear(h_size, self.action_dim))

        self.train()

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = F.relu(self.fc3(x))
        x = F.relu(self.fc4(x))
        x = F.relu(self.fc5(x))
        x = torch.tanh(self.out(x))
        return x


class SoftsignActor(nn.Module):
    """ Simple neural net actor that takes observation as input and outputs torques """

    def __init__(self, env):
        super(SoftsignActor, self).__init__()
        self.state_dim = env.observation_space.shape[0]
        self.action_dim = env.action_space.shape[0]

        init_s_ = lambda m: init(
            m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain("sigmoid"),
        )
        init_r_ = lambda m: init(
            m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain("relu"),
        )
        init_t_ = lambda m: init(
            m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain("tanh"),
        )

        h_size = 256
        self.fc1 = init_s_(nn.Linear(self.state_dim, h_size))
        self.fc2 = init_s_(nn.Linear(h_size, h_size))
        self.fc3 = init_s_(nn.Linear(h_size, h_size))
        self.fc4 = init_r_(nn.Linear(h_size, h_size))
        self.fc5 = init_r_(nn.Linear(h_size, h_size))
        self.out = init_t_(nn.Linear(h_size, self.action_dim))

        self.train()

    def forward(self, x):
        x = F.softsign(self.fc1(x))
        x = F.softsign(self.fc2(x))
        x = F.softsign(self.fc3(x))
        x = F.relu(self.fc4(x))
        x = F.relu(self.fc5(x))
        x = torch.tanh(self.out(x))
        return x


def mini_weight_init(m):
    if m.__class__.__name__ == "Linear":
        m.weight.data.copy_(nn.init.uniform_(m.weight.data, -3e-3, 3e-3))
        m.bias.data.fill_(0)


class PolNet(nn.Module):
    def __init__(self, observation_space, action_space, deterministic=False):
        super(PolNet, self).__init__()

        self.deterministic = deterministic

        if isinstance(action_space, gym.spaces.Box):
            self.discrete = False
        else:
            self.discrete = True
            if isinstance(action_space, gym.spaces.MultiDiscrete):
                self.multi = True
            else:
                self.multi = False

        init_ = lambda m: init(
            m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain("relu"),
        )

        h_size = 256
        self.fc1 = init_(nn.Linear(observation_space.shape[0], h_size))
        self.fc2 = init_(nn.Linear(h_size, h_size))
        self.fc3 = init_(nn.Linear(h_size, h_size))
        self.fc4 = init_(nn.Linear(h_size, h_size))
        self.fc5 = init_(nn.Linear(h_size, h_size))

        if not self.discrete:
            self.mean_layer = nn.Linear(h_size, action_space.shape[0])
            if not self.deterministic:
                self.log_std_param = nn.Parameter(
                    torch.randn(action_space.shape[0]) * 1e-10 - 1
                )
            self.mean_layer.apply(mini_weight_init)
        else:
            if self.multi:
                self.output_layers = nn.ModuleList(
                    [nn.Linear(h_size, vec) for vec in action_space.nvec]
                )
                list(map(lambda x: x.apply(mini_weight_init), self.output_layers))
            else:
                self.output_layer = nn.Linear(h_size, action_space.n)
                self.output_layer.apply(mini_weight_init)

    def forward(self, ob):
        h = F.softsign(self.fc1(ob))
        h = F.softsign(self.fc2(h))
        h = F.softsign(self.fc3(h))
        h = F.relu(self.fc4(h))
        h = F.relu(self.fc5(h))
        if not self.discrete:
            mean = torch.tanh(self.mean_layer(h))
            if not self.deterministic:
                log_std = self.log_std_param.expand_as(mean)
                return mean, log_std
            else:
                return mean
        else:
            if self.multi:
                return torch.cat(
                    [
                        torch.softmax(ol(h), dim=-1).unsqueeze(-2)
                        for ol in self.output_layers
                    ],
                    dim=-2,
                )
            else:
                return torch.softmax(self.output_layer(h), dim=-1)


class VNet(nn.Module):
    def __init__(self, observation_space):
        super(VNet, self).__init__()

        init_ = lambda m: init(
            m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain("relu"),
        )

        h_size = 256
        self.fc1 = init_(nn.Linear(observation_space.shape[0], h_size))
        self.fc2 = init_(nn.Linear(h_size, h_size))
        self.fc3 = init_(nn.Linear(h_size, h_size))
        self.fc4 = init_(nn.Linear(h_size, h_size))
        self.output_layer = init_(nn.Linear(h_size, 1))

    def forward(self, ob):
        h = F.relu(self.fc1(ob))
        h = F.relu(self.fc2(h))
        h = F.relu(self.fc3(h))
        h = F.relu(self.fc4(h))
        return self.output_layer(h)
