import os
import time
import functools
import numpy as np
import os.path as osp
import tensorflow as tf
from baselines import logger
from collections import deque
from baselines.common import explained_variance, set_global_seeds
from baselines.common.policies import build_policy
from baselines.common.runners import AbstractEnvRunner
from baselines.common.tf_util import get_session, save_variables, load_variables
from baselines.common.running_mean_std import RunningMeanStd
try:
    from baselines.common.mpi_adam_optimizer import MpiAdamOptimizer
    from mpi4py import MPI
    from baselines.common.mpi_util import sync_from_root
except ImportError:
    MPI = None

from baselines.common.tf_util import initialize

class Model(object):
    """
    We use this object to :
    __init__:
    - Creates the step_model
    - Creates the train_model

    train():
    - Make the training part (feedforward and retropropagation of gradients)

    save/load():
    - Save load the model
    """
    def __init__(self, *, policy, ob_space, ac_space, nbatch_act, nbatch_train,
                nsteps, ent_coef, vf_coef, max_grad_norm, adaptive_kl):
        sess = get_session()

        with tf.variable_scope('ppo2_model', reuse=tf.AUTO_REUSE):
            # CREATE OUR TWO MODELS
            # act_model that is used for sampling
            act_model = policy(nbatch_act, 1, sess)

            # Train model for training
            train_model = policy(None, nsteps, sess)

        # CREATE THE PLACEHOLDERS
        A = train_model.pdtype.sample_placeholder([None])
        MEANNOW = train_model.pdtype.sample_placeholder([None])
        LOGSTDNOW = train_model.pdtype.sample_placeholder([None])
        ADV = tf.placeholder(tf.float32, [None])
        R = tf.placeholder(tf.float32, [None])
        # Keep track of old actor
        OLDNEGLOGPAC = tf.placeholder(tf.float32, [None])
        NEGLOGPACNOW = tf.placeholder(tf.float32, [None])
        RHO_NOW = tf.placeholder(tf.float32, [None])
        # Keep track of old critic
        OLDVPRED = tf.placeholder(tf.float32, [None])
        LR = tf.placeholder(tf.float32, [])
        # Cliprange
        CLIPRANGE = tf.placeholder(tf.float32, [])
        KLCONST = tf.placeholder(tf.float32, [])
        KL_REST = tf.placeholder(tf.float32, [None])

        neglogpac = train_model.pd.neglogp(A)
        mean = train_model.pd.mean
        logstd = train_model.pd.logstd

        # Calculate the entropy
        # Entropy is used to improve exploration by limiting the premature convergence to suboptimal policy.
        entropy = tf.reduce_mean(train_model.pd.entropy())

        # CALCULATE THE LOSS
        # Total loss = Policy gradient loss - entropy * entropy coefficient + Value coefficient * value loss

        # Clip the value to reduce variability during Critic training
        # Get the predicted value
        vpred = train_model.vf
        vpredclipped = OLDVPRED + tf.clip_by_value(train_model.vf - OLDVPRED, - CLIPRANGE, CLIPRANGE)
        # Unclipped value
        vf_losses1 = tf.square(vpred - R)
        # Clipped value
        vf_losses2 = tf.square(vpredclipped - R)

        vf_loss = .5 * tf.reduce_mean(tf.maximum(vf_losses1, vf_losses2))

        # Calculate ratio (pi current policy / pi old policy)
        ratio = tf.exp(OLDNEGLOGPAC - neglogpac)  # * tf.minimum(1.0,RHO_NOW)

        # Defining Loss = - J is equivalent to max J
        pg_losses = -ratio * ADV  # * tf.minimum(1.0,RHO_NOW)

        pg_losses2 = -tf.clip_by_value(ratio, 1.0 - CLIPRANGE, 1.0 + CLIPRANGE) * ADV  # * tf.minimum(1.0,RHO_NOW)

        # Final PG loss
        # pg_loss = tf.reduce_mean(tf.stop_gradient(tf.maximum(pg_losses, pg_losses2))*(-neglogpac)) + .5 * tf.reduce_mean(tf.square(neglogpac - OLDNEGLOGPAC))


        approxkl = .5 * tf.reduce_mean(tf.square(neglogpac - OLDNEGLOGPAC) * KL_REST)
        approxklold = .5 * tf.reduce_mean(tf.square(neglogpac - OLDNEGLOGPAC))
        approxklnow = .5 * tf.reduce_mean(tf.square(neglogpac - NEGLOGPACNOW) * tf.minimum(1.0,RHO_NOW))
        kloldnew = tf.reduce_mean(tf.reduce_sum(
            logstd - LOGSTDNOW + 0.5 * (tf.square(tf.exp(LOGSTDNOW)) + tf.square(mean - MEANNOW)) / tf.square(
                tf.exp(logstd)) - 0.5, axis=1))
        clipfrac = tf.reduce_mean(tf.to_float(tf.greater(tf.abs(ratio - 1.0), CLIPRANGE)))
        pg_loss = tf.reduce_mean(tf.maximum(pg_losses, pg_losses2))  # * tf.minimum(1.0,RHO_NOW))
        # Total loss# * tf.minimum(1.0,RHO_NOW))
        if adaptive_kl:
            pg_loss = pg_loss + KLCONST*approxkl
        # Total loss
        loss = pg_loss - entropy * ent_coef + vf_loss * vf_coef

        # UPDATE THE PARAMETERS USING LOSS
        # 1. Get the model parameters
        params = tf.trainable_variables('ppo2_model')
        print(params)
        # 2. Build our trainer
        if MPI is not None:
            trainer = MpiAdamOptimizer(MPI.COMM_WORLD, learning_rate=LR, epsilon=1e-5)
        else:
            trainer = tf.train.AdamOptimizer(learning_rate=LR, epsilon=1e-5)
        # 3. Calculate the gradients
        grads_and_var = trainer.compute_gradients(loss, params)
        grads, var = zip(*grads_and_var)

        if max_grad_norm is not None:
            # Clip the gradients (normalize)
            grads, _grad_norm = tf.clip_by_global_norm(grads, max_grad_norm)
        _grad_norm = tf.sqrt(tf.reduce_sum([tf.norm(grad) ** 2 for grad in grads]))
        grads_and_var = list(zip(grads, var))
        # zip aggregate each gradient with parameters associated
        # For instance zip(ABCD, xyza) => Ax, By, Cz, Da

        _train = trainer.apply_gradients(grads_and_var)

        def train(lr, cliprange, klconst, rgae, trunc_rho, obs, returns, advs, masks, actions, values, neglogpacs, mean_now, logstd_now, kl_rest, rho_now, neglogpnow, states=None):
            # Here we calculate advantage A(s,a) = R + yV(s') - V(s)
            # Returns = R + yV(s')
            # Normalize the advantages
            advs = (advs - advs.mean()) / (advs.std() + 1e-8)
            td_map = {train_model.X:obs, A:actions, ADV:advs, R:returns, LR:lr,
                    CLIPRANGE:cliprange, OLDNEGLOGPAC:neglogpacs, OLDVPRED:values, MEANNOW:mean_now, LOGSTDNOW:logstd_now, KLCONST:klconst, KL_REST:kl_rest, RHO_NOW:rho_now, NEGLOGPACNOW:neglogpnow}
            if states is not None:
                td_map[train_model.S] = states
                td_map[train_model.M] = masks
            return sess.run(
                [pg_loss, vf_loss, entropy, approxkl, clipfrac, kloldnew, approxklold, approxklnow, _grad_norm,  _train],
                td_map
            )[:-1]
        self.loss_names = ['policy_loss', 'value_loss', 'policy_entropy', 'approxkl', 'clipfrac', 'kloldnew', 'approxklold', 'approxklnow', 'gradnorm']


        self.train = train
        self.train_model = train_model
        self.act_model = act_model
        self.step = act_model.step
        self.meanlogstd = act_model.meanlogstd
        self.value = act_model.value
        self.values = train_model.value
        self.meanlogstds = train_model.meanlogstd
        self.initial_state = act_model.initial_state

        self.save = functools.partial(save_variables, sess=sess)
        self.load = functools.partial(load_variables, sess=sess)

        if MPI is None or MPI.COMM_WORLD.Get_rank() == 0:
            initialize()
        global_variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope="")

        if MPI is not None:
            sync_from_root(sess, global_variables) #pylint: disable=E1101

class Runner(AbstractEnvRunner):
    """
    We use this object to make a mini batch of experiences
    __init__:
    - Initialize the runner

    run():
    - Make a mini batch
    """
    def __init__(self, *, env, model, nsteps, gamma, lam):
        super().__init__(env=env, model=model, nsteps=nsteps)
        # Lambda used in GAE (General Advantage Estimation)
        self.lam = lam
        # Discount rate
        self.gamma = gamma
        self.clipob = 10.
        self.cliprew = 10.
        self.epsilon = 1e-8
        self.ret = np.zeros(env.num_envs)
        self.ob_rms = RunningMeanStd(shape=self.env.observation_space.shape)
        self.ret_rms = RunningMeanStd(shape=())

    def obfilt(self, obs):
        obs = np.clip((obs - self.ob_rms.mean) / np.sqrt(self.ob_rms.var + self.epsilon), -self.clipob,
                      self.clipob)
        return obs

    def rewfilt(self, rews):
        rews = np.clip(rews / np.sqrt(self.ret_rms.var + self.epsilon), -self.cliprew, self.cliprew)
        return rews

    def run(self):
        # Here, we init the lists that will contain the mb of experiences
        mb_obs, mb_rewards, mb_actions, mb_values, mb_dones, mb_neglogpacs , mb_means , mb_logstds = [],[],[],[],[],[],[],[]
        mb_states = self.states
        epinfos = []
        # For n in range number of steps
        for _ in range(self.nsteps):
            # Given observations, get action value and neglopacs
            # We already have self.obs because Runner superclass run self.obs[:] = env.reset() on init
            actions, values, self.states, neglogpacs = self.model.step(self.obfilt(self.obs), S=self.states, M=self.dones)
            means, logstds = self.model.meanlogstd(self.obfilt(self.obs))
            mb_obs.append(self.obs.copy())
            mb_actions.append(actions)
            mb_values.append(values)
            mb_neglogpacs.append(neglogpacs)
            mb_dones.append(self.dones)
            mb_means.append(means)
            mb_logstds.append(logstds)

            # Take actions in env and look the results
            # Infos contains a ton of useful informations
            self.obs[:], rewards, self.dones, infos = self.env.step(actions)
            self.ob_rms.update(self.obs)
            self.ret =  self.ret * self.gamma + rewards
            self.ret_rms.update(self.ret)
            for i in range(len(self.dones)):
                if self.dones[i]:
                    self.ret[i] = 0

            for info in infos:
                maybeepinfo = info.get('episode')
                if maybeepinfo: epinfos.append(maybeepinfo)
            mb_rewards.append(rewards)
        #batch of steps to batch of rollouts
        mb_obs = np.asarray(mb_obs, dtype=self.obs.dtype)
        mb_rewards = np.asarray(mb_rewards, dtype=np.float32)
        mb_actions = np.asarray(mb_actions)
        mb_means = np.asarray(mb_means)
        mb_logstds = np.asarray(mb_logstds)
        # mb_values = np.asarray(mb_values, dtype=np.float32)
        mb_neglogpacs = np.asarray(mb_neglogpacs, dtype=np.float32)
        mb_dones = np.asarray(mb_dones, dtype=np.bool)
        # last_values = self.model.value(self.obs, S=self.states, M=self.dones)
        # discount/bootstrap off value fn
        # mb_returns = np.zeros_like(mb_rewards)
        # mb_advs = np.zeros_like(mb_rewards)
        # lastgaelam = 0
        # for t in reversed(range(self.nsteps)):
        #     if t == self.nsteps - 1:
        #         nextnonterminal = 1.0 - self.dones
        #         nextvalues = last_values
        #     else:
        #         nextnonterminal = 1.0 - mb_dones[t+1]
        #         nextvalues = mb_values[t+1]
        #     delta = mb_rewards[t] + self.gamma * nextvalues * nextnonterminal - mb_values[t]
        #     mb_advs[t] = lastgaelam = delta + self.gamma * self.lam * nextnonterminal * lastgaelam
        # mb_returns = mb_advs + mb_values
        return mb_obs, mb_rewards, mb_dones, mb_actions, mb_neglogpacs, mb_means, mb_logstds, self.obs, self.dones, epinfos

class EvalRunner(AbstractEnvRunner):
    """
    We use this object to make a mini batch of experiences
    __init__:
    - Initialize the runner

    run():
    - Make a mini batch
    """

    def __init__(self, *, env, model, nsteps, gamma, lam):
        super().__init__(env=env, model=model, nsteps=nsteps)
        # Lambda used in GAE (General Advantage Estimation)
        self.lam = lam
        # Discount rate
        self.gamma = gamma
        self.obfilt = None
        self.rewfilt = None
        self.env=env

    def run(self):
        # Here, we init the lists that will contain the mb of experiences
        mb_obs, mb_rewards, mb_actions, mb_values, mb_dones, mb_neglogpacs, mb_means, mb_logstds = [], [], [], [], [], [], [], []
        mb_states = self.states
        epinfos = []
        # For n in range number of steps
        epi_lens=0
        self.obs=self.env.reset()
        for _ in range(self.nsteps):
            # Given observations, get action value and neglopacs
            # We already have self.obs because Runner superclass run self.obs[:] = env.reset() on init
            actions, logstds = self.model.meanlogstd(self.obfilt(self.obs))
            mb_obs.append(self.obs.copy())
            mb_actions.append(actions)
            mb_dones.append(self.dones)

            # Take actions in env and look the results
            # Infos contains a ton of useful informations
            self.obs[:], rewards, self.dones, infos = self.env.step(actions)

            for info in infos:
                maybeepinfo = info.get('episode')
                if maybeepinfo: epinfos.append(maybeepinfo)
            mb_rewards.append(rewards)
            for i in range(len(self.dones)):
                if self.dones[i]:
                    epi_lens+=1
            if epi_lens>=10:
                break
        # batch of steps to batch of rollouts
        mb_obs = np.asarray(mb_obs, dtype=self.obs.dtype)
        mb_rewards = np.asarray(mb_rewards, dtype=np.float32)
        mb_actions = np.asarray(mb_actions)
        # mb_values = np.asarray(mb_values, dtype=np.float32)
        mb_dones = np.asarray(mb_dones, dtype=np.bool)
        # last_values = self.model.value(self.obs, S=self.states, M=self.dones)
        # discount/bootstrap off value fn
        # mb_returns = np.zeros_like(mb_rewards)
        # mb_advs = np.zeros_like(mb_rewards)
        # lastgaelam = 0
        # for t in reversed(range(self.nsteps)):
        #     if t == self.nsteps - 1:
        #         nextnonterminal = 1.0 - self.dones
        #         nextvalues = last_values
        #     else:
        #         nextnonterminal = 1.0 - mb_dones[t+1]
        #         nextvalues = mb_values[t+1]
        #     delta = mb_rewards[t] + self.gamma * nextvalues * nextnonterminal - mb_values[t]
        #     mb_advs[t] = lastgaelam = delta + self.gamma * self.lam * nextnonterminal * lastgaelam
        # mb_returns = mb_advs + mb_values
        return (*map(sf01, (mb_obs, mb_rewards, mb_dones, mb_actions)),
                self.obs, self.dones, epinfos)
# obs, returns, masks, actions, values, neglogpacs, states = runner.run()
def sf01(arr):
    """
    swap and then flatten axes 0 and 1
    """
    s = arr.shape
    return arr.swapaxes(0, 1).reshape(s[0] * s[1], *s[2:])

def constfn(val):
    def f(_):
        return val
    return f

def learn(*, network, env, total_timesteps, dtarg=0.01, adaptive_kl=0, trunc_rho=1.0, clipcut=0.2, useadv = 0, vtrace = 0, rgae=0, eval_env = None, seed=None, ERlen=1, nsteps=2048, ent_coef=0.0, lr=3e-4,
            vf_coef=0.5,  max_grad_norm=None, gamma=0.99, lam=0.95,
            log_interval=10, nminibatches=4, noptepochs=4, cliprange=0.2,
            save_interval=0, load_path=None, **network_kwargs):
    '''
    Learn policy using PPO algorithm (https://arxiv.org/abs/1707.06347)

    Parameters:
    ----------

    network:                          policy network architecture. Either string (mlp, lstm, lnlstm, cnn_lstm, cnn, cnn_small, conv_only - see baselines.common/models.py for full list)
                                      specifying the standard network architecture, or a function that takes tensorflow tensor as input and returns
                                      tuple (output_tensor, extra_feed) where output tensor is the last network layer output, extra_feed is None for feed-forward
                                      neural nets, and extra_feed is a dictionary describing how to feed state into the network for recurrent neural nets.
                                      See common/models.py/lstm for more details on using recurrent nets in policies

    env: baselines.common.vec_env.VecEnv     environment. Needs to be vectorized for parallel environment simulation.
                                      The environments produced by gym.make can be wrapped using baselines.common.vec_env.DummyVecEnv class.


    nsteps: int                       number of steps of the vectorized environment per update (i.e. batch size is nsteps * nenv where
                                      nenv is number of environment copies simulated in parallel)

    total_timesteps: int              number of timesteps (i.e. number of actions taken in the environment)

    ent_coef: float                   policy entropy coefficient in the optimization objective

    lr: float or function             learning rate, constant or a schedule function [0,1] -> R+ where 1 is beginning of the
                                      training and 0 is the end of the training.

    vf_coef: float                    value function loss coefficient in the optimization objective

    max_grad_norm: float or None      gradient norm clipping coefficient

    gamma: float                      discounting factor

    lam: float                        advantage estimation discounting factor (lambda in the paper)

    log_interval: int                 number of timesteps between logging events

    nminibatches: int                 number of training minibatches per update. For recurrent policies,
                                      should be smaller or equal than number of environments run in parallel.

    noptepochs: int                   number of training epochs per update

    cliprange: float or function      clipping range, constant or schedule function [0,1] -> R+ where 1 is beginning of the training
                                      and 0 is the end of the training

    save_interval: int                number of timesteps between saving events

    load_path: str                    path to load the model from

    **network_kwargs:                 keyword arguments to the policy / network builder. See baselines.common/policies.py/build_policy and arguments to a particular type of network
                                      For instance, 'mlp' network architecture has arguments num_hidden and num_layers.



    '''

    set_global_seeds(seed)

    if isinstance(lr, float): lr = constfn(lr)
    else: assert callable(lr)
    if isinstance(cliprange, float): cliprange = constfn(cliprange)
    else: assert callable(cliprange)
    total_timesteps = int(total_timesteps)

    policy = build_policy(env, network, **network_kwargs)

    # Get the nb of env
    nenvs = env.num_envs

    # Get state_space and action_space
    ob_space = env.observation_space
    ac_space = env.action_space
    acdim = ac_space.shape[0]
    obdim = ob_space.shape[0]

    # Calculate the batch_size
    nbatch = nenvs * nsteps
    nbatch_train = nbatch // nminibatches

    # Instantiate the model object (that creates act_model and train_model)
    make_model = lambda : Model(policy=policy, ob_space=ob_space, ac_space=ac_space, nbatch_act=nenvs, nbatch_train=nbatch_train,
                    nsteps=nsteps, ent_coef=ent_coef, vf_coef=vf_coef,
                    max_grad_norm=max_grad_norm, adaptive_kl=adaptive_kl)
    model = make_model()
    if load_path is not None:
        model.load(load_path)
    # Instantiate the runner object
    runner = Runner(env=env, model=model, nsteps=nsteps, gamma=gamma, lam=lam)
    if eval_env is not None:
        eval_runner = EvalRunner(env = eval_env, model = model, nsteps = 10*nsteps, gamma = gamma, lam= lam)
        eval_runner.obfilt=runner.obfilt
        eval_runner.rewfilt=runner.rewfilt



    epinfobuf = deque(maxlen=10)
    if eval_env is not None:
        eval_epinfobuf = deque(maxlen=10)

    # Start total timer
    tfirststart = time.time()

    nupdates = total_timesteps//nbatch

    def add_vtarg_and_adv(seg, gamma, value, lam):
        """
        Compute target value using TD(lambda) estimator, and advantage with GAE(lambda)
        """
        num_samples = np.size(seg["done"])
        done = np.vstack([seg["done"],np.zeros((1,nenvs))])  # last element is only used for last vtarg, but we already zeroed it if last new = 1

        T = int(num_samples/nenvs)
        gaelam = np.empty_like(seg["rew"])
        rew = runner.rewfilt(seg["rew"])
        lastgaelam = np.zeros(nenvs)
        for t in reversed(range(T)):
            nonterminal = 1 - done[t + 1,:]
            delta = rew[t,:] + gamma * value[t + 1,:] * nonterminal - value[t,:]
            gaelam[t,:] = lastgaelam = delta + gamma * lam * nonterminal * lastgaelam
        ret = gaelam + value[:-1,:]
        return ret, gaelam

    def add_vtarg_and_adv_vtrace(seg, gamma, value, rho, trunc_rho, acdim=None):
        """
        Compute target value using TD(lambda) estimator, and advantage with GAE(lambda)
        """

        num_samples = np.size(seg["done"])
        done = np.vstack([seg["done"], np.zeros((1, nenvs))])  # last element is only used for last vtarg, but we already zeroed it if last new = 1
        r = np.minimum(1.0,rho)
        T = int(num_samples / nenvs)
        gaelam = np.empty_like(seg["rew"])
        rew = runner.rewfilt(seg["rew"])
        lastgaelam = np.zeros(nenvs)
        for t in reversed(range(T)):
            nonterminal = 1 - done[t + 1, :]
            delta = rew[t, :] + gamma * value[t + 1, :] * nonterminal - value[t, :]
            gaelam[t, :] = delta + gamma * lam * nonterminal * lastgaelam
            lastgaelam = r[t, :] * gaelam[t, :]
        ret = r * gaelam + value[:-1,:]
        return ret, gaelam

    seg = None
    cliprangenow = cliprange(1.0)
    klconst=1.0
    for update in range(1, nupdates+1):
        assert nbatch % nminibatches == 0
        # Start timer
        tstart = time.time()
        frac = 1.0 - (update - 1.0) / nupdates
        # Calculate the learning rate
        lrnow = lr(frac)
        # Calculate the cliprange

        # Get minibatch
        if seg is None:
            prev_seg = seg
            seg = {}
        else:
            prev_seg = {}
            for i in seg:
                prev_seg[i] = np.copy(seg[i])
        seg["ob"], seg["rew"], seg["done"], seg["ac"], seg["neglogp"], seg["mean"], seg["logstd"], final_obs, final_done, epinfos = runner.run() #pylint: disable=E0632
        # print(np.shape(seg["ob"]))
        # print(np.shape(seg["rew"]))
        # print(np.shape(final_obs))
        # print(np.shape(final_done))
        if prev_seg is not None:
            for key in seg:
                seg[key] = np.vstack([prev_seg[key], seg[key]])
                if np.shape(seg[key])[0] > ERlen * nsteps:
                    seg[key] = seg[key][-ERlen * nsteps:]

        ob = runner.obfilt(seg["ob"])
        values = model.values(ob).reshape((-1,nenvs))
        final_values = ((1.0-final_done) * model.values(runner.obfilt(final_obs))).reshape((-1,nenvs))
        # print(np.shape(ob))
        # print(np.shape(values))
        # print(np.shape(final_values))
        values = np.vstack([values,final_values])
        # print(np.shape(values))
        mean_now, logstd_now = model.meanlogstds(ob)
        mean_now = mean_now.reshape(-1,nenvs,acdim)
        logstd_now = logstd_now.reshape(-1,nenvs,acdim)
        # print(np.shape(seg["ac"])[1])
        neglogpnow = 0.5 * np.sum(np.square((seg["ac"] - mean_now) / np.exp(logstd_now)), axis=-1) \
                      + 0.5 * np.log(2.0 * np.pi) * np.shape(seg["ac"])[-1] \
                      + np.sum(logstd_now, axis=-1)
        # print(np.shape(neglogpnow))
        # print(np.shape(seg["neglogp"]))
        rho = np.exp(-neglogpnow + seg["neglogp"])
        print(rho)
        # print(np.shape(rho))
        # print(len(mean_now))
        # print(cliprangenow)
        # print(rho)
        if vtrace==1:
            ret, gae = add_vtarg_and_adv_vtrace(seg, gamma, values, rho, trunc_rho)
        else:
            ret, gae = add_vtarg_and_adv(seg, gamma, values, lam)
        r = np.minimum(1.0, rho)
        r_gae = gae * r
        print("======")
        print(gae)
        # print(r_gae)
        # print(gae.mean())
        # print(r_gae.mean())
        # print(gae.std())
        # print(r_gae.std())
        # print(r.mean())
        print("======")

        if eval_env is not None:
            eval_obs, eval_returns, eval_masks, eval_actions, _,_, eval_epinfos = eval_runner.run() #pylint: disable=E0632

        rho_after =  np.exp(- 0.5 * np.square((seg["ac"] - mean_now) / np.exp(logstd_now)) \
                     - logstd_now + 0.5 * np.square((seg["ac"] - seg["mean"]) / np.exp(seg["logstd"]))\
                     + seg["logstd"])
        print(np.shape(rho_after))

        prior_ = np.zeros((len(ob),nenvs))
        temp_ = np.zeros(int(len(prior_) / nsteps))
        for i in range(nenvs):
            temp_prior = []
            for j in range(int(len(prior_) / nsteps)):
                temp_row = np.mean(np.abs(rho_after[j * nsteps:(j + 1) * nsteps,i,:] - 1.0) + 1.0)
                # local_rho[i + (ERlen-int(len(prior_row)/nsteps))].append(temp_row)
                if temp_row > 1 + clipcut:
                    prior_[j * nsteps:(j + 1) * nsteps, i] = 0
                else:
                    prior_[j * nsteps:(j + 1) * nsteps, i] = 1
                    # prior_row[i * nsteps:(i + 1) * nsteps] = 1
                temp_prior.append(temp_row)
            temp_ += temp_prior
        temp_ /= nenvs
        print(temp_)

        # for i in range(len(prior_row)):
        #     if (np.abs(rho[i] - 1.0) + 1.0)>1.05:
        #         prior_row[i]=0
        #     else:
        #         prior_row[i]=1
        # for i in range(len(prior_row)):
        #     if rho[i]>1.1 :
        #         prior_row[i]=0
        #     else:
        #         prior_row[i]=1
        # prob = prior_row/np.sum(prior_row)

        print(np.sum(prior_,axis=0))


        epinfobuf.extend(epinfos)
        if eval_env is not None:
            eval_epinfobuf.extend(eval_epinfos)
        print(eval_epinfos)

        # Here what we're going to do is for each minibatch calculate the loss and append it.
        mblossvals = []
        # Index of each element of batch_size
        # Create the indices array

        inds1 = np.arange(len(seg["ob"]) - nsteps)
        inds2 = np.arange(nsteps)+len(seg["ob"]) - nsteps
        print(len(seg["ob"]))
        print(cliprangenow)
        nbatch_adapt1 = (np.sum(prior_, axis=0) - nsteps) / nsteps * nbatch_train / nenvs
        nbatch_adapt2 = int((nsteps) / nsteps * nbatch_train / nenvs)
        # print(nbatch_adapt2)
        # print(rho)
        idx1=[]
        idx2=[]
        kl_rest = np.ones_like(seg["rew"]) * np.sum(prior_, axis=0) / nsteps
        kl_rest[:-nsteps,:]=0
        # print(kl_rest)

        for _ in range(noptepochs):
            # Randomize the indexes
            # np.random.shuffle(inds)
            # 0 to batch_size with batch_train_size step

            # print(nbatch_adapt)
            losses_epoch = []
            for _ in range(int(nsteps/(nbatch_train/nenvs))):
                ind1d = []
                ind2d = []
                for i in range(nenvs):
                    if nbatch_adapt1[i]>0:
                        idx1 = np.random.choice(inds1, int(nbatch_adapt1[i]), p=prior_[:-nsteps,i]/np.sum(prior_[:-nsteps,i]))
                    idx2 = np.random.choice(inds2, nbatch_adapt2)
                    # print(np.mean(np.abs(rho[mbinds] - 1.0) + 1.0))
                    idx = np.hstack([idx1,idx2]).astype(int)
                    for d in idx:
                        ind1d.append(d)
                        ind2d.append(i)
                slices = (arr[(np.array(ind1d),np.array(ind2d))] for arr in (ob, ret, gae, seg["done"], seg["ac"], values[:-1,:], seg["neglogp"], seg["mean"], seg["logstd"], kl_rest, rho, neglogpnow))
                loss_epoch = model.train(lrnow, cliprangenow, klconst, rgae, trunc_rho, *slices)
                mblossvals.append(loss_epoch)
                losses_epoch.append(loss_epoch)


        # Feedforward --> get losses --> update
        lossvals = np.mean(mblossvals, axis=0)
        if adaptive_kl:
            print("KL avg :", lossvals[3])
            if lossvals[3] > dtarg * 1.5:
                klconst *= 2
                print("kl const is increased")
            elif lossvals[3] < dtarg / 1.5:
                klconst /= 2
                print("kl const is reduced")
            klconst = np.clip(klconst,2**(-10),64)
        # End timer
        tnow = time.time()
        # Calculate the fps (frame per second)
        fps = int(nbatch / (tnow - tstart))
        if update % log_interval == 0 or update == 1:
            # Calculates if value function is a good predicator of the returns (ev > 1)
            # or if it's just worse than predicting nothing (ev =< 0)
            ev = explained_variance(values[:-1,:].reshape(-1), ret.reshape(-1))
            logger.logkv("batch IS weight", [int(1000*s)/1000. for s in np.array(temp_)])
            logger.logkv("kl const", klconst)
            logger.logkv("clipping factor", cliprangenow)
            logger.logkv("learning rate", lrnow)
            logger.logkv("serial_timesteps", update*nsteps)
            logger.logkv("nupdates", update)
            logger.logkv("total_timesteps", update*nbatch)
            logger.logkv("fps", fps)
            logger.logkv("explained_variance", float(ev))
            logger.logkv('eprewmean', safemean([epinfo['r'] for epinfo in epinfobuf]))
            logger.logkv('eplenmean', safemean([epinfo['l'] for epinfo in epinfobuf]))
            if eval_env is not None:
                logger.logkv('eval_eprewmean', safemean([epinfo['r'] for epinfo in eval_epinfos]) )
                logger.logkv('eval_eplenmean', safemean([epinfo['l'] for epinfo in eval_epinfos]) )
            logger.logkv('time_elapsed', tnow - tfirststart)
            for (lossval, lossname) in zip(lossvals, model.loss_names):
                logger.logkv(lossname, lossval)
            if MPI is None or MPI.COMM_WORLD.Get_rank() == 0:
                logger.dumpkvs()
        if save_interval and (update % save_interval == 0 or update == 1) and logger.get_dir() and (MPI is None or MPI.COMM_WORLD.Get_rank() == 0):
            checkdir = osp.join(logger.get_dir(), 'checkpoints')
            os.makedirs(checkdir, exist_ok=True)
            savepath = osp.join(checkdir, '%.5i'%update)
            print('Saving to', savepath)
            model.save(savepath)
    return model
# Avoid division error when calculate the mean (in our case if epinfo is empty returns np.nan, not return an error)
def safemean(xs):
    return np.nan if len(xs) == 0 else np.mean(xs)



