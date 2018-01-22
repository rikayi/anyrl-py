"""
Distributional Q-learning models.
"""

from abc import abstractmethod
from math import log

import numpy as np
import tensorflow as tf

from .base import TFQNetwork
from .util import take_vector_elems, put_vector_elems

# pylint: disable=R0913

class DistQNetwork(TFQNetwork):
    """
    An abstract Q-network that predicts action-conditional
    reward distributions (as opposed to expectations).

    Subclasses should override the base() and value_func()
    methods with specific neural network architectures.
    """
    def __init__(self, session, num_actions, obs_vectorizer, name, num_atoms, min_val, max_val,
                 dueling=False, dense=tf.layers.dense):
        super(DistQNetwork).__init__(self, session, num_actions, obs_vectorizer, name)
        self.dueling = dueling
        self.dense = dense
        self.dist = DiscreteDist(num_atoms, min_val, max_val)
        old_vars = tf.trainable_variables()
        with tf.variable_scope(name):
            self.step_obs_ph = tf.placeholder(self.input_dtype,
                                              shape=(None,) + obs_vectorizer.out_shape)
            self.step_base_out = self.base(self.step_obs_ph)
            log_probs = self.value_func(self.step_base_out)
            values = self.dist.mean(log_probs)
            self.step_outs = (values, log_probs)
        self.variables = [v for v in tf.trainable_variables() if v not in old_vars]

    def step(self, observations, states):
        feed = self.step_feed_dict(observations, states)
        values, dists = self.session.run(self.step_outs, feed_dict=feed)
        return {
            'actions': np.argmax(values, axis=1),
            'states': None,
            'action_values': values,
            'action_dists': dists
        }

    def transition_loss(self, target_net, obses, actions, rews, new_obses, terminals, discounts):
        with tf.variable_scope(self.name, reuse=True):
            max_actions = tf.argmax(self.dist.mean(self.value_func(self.base(new_obses))),
                                    axis=1, output_type=tf.int32)
        with tf.variable_scope(target_net.name, reuse=True):
            target_preds = target_net.value_func(target_net.base(new_obses))
            target_preds = tf.where(terminals, tf.zeros_like(target_preds) - log(self.num_actions),
                                    target_preds)
        target_dists = self.dist.add_rewards(take_vector_elems(target_preds, max_actions),
                                             rews, discounts)
        with tf.variable_scope(self.name, reuse=True):
            online_preds = self.value_func(self.base(obses))
            onlines = take_vector_elems(online_preds, actions)
            return _kl_divergence(tf.stop_gradient(target_dists), onlines)

    @abstractmethod
    def base(self, obs_batch):
        """
        Go from a Tensor of observations to a Tensor of
        feature vectors to feed into the output heads.

        Returns:
          A Tensor of shape [batch_size x num_features].
        """
        pass

    def value_func(self, feature_batch):
        """
        Go from a 2-D Tensor of feature vectors to a 3-D
        Tensor of predicted action distributions.

        Args:
          feature_batch: a batch of features from base().

        Returns:
          A Tensor of shape [batch x actions x atoms].

        All probabilities are computed in the log domain.
        """
        logits = self.dense(feature_batch, self.num_actions * self.dist.num_atoms)
        actions = tf.reshape(logits, (tf.shape(logits)[0], self.num_actions, self.dist.num_atoms))
        if not self.dueling:
            return tf.nn.log_softmax(actions)
        values = tf.expand_dims(self.dense(feature_batch, self.dist.num_atoms), axis=1)
        actions -= tf.reduce_mean(actions, axis=1, keep_dims=True)
        return values + actions

    # pylint: disable=W0613
    def step_feed_dict(self, observations, states):
        """Produce a feed_dict for taking a step."""
        return {self.step_obs_ph: self.obs_vectorizer.to_vecs(observations)}

class DiscreteDist:
    """
    A discrete reward distribution.
    """
    def __init__(self, num_atoms, min_val, max_val):
        assert num_atoms >= 2
        assert max_val > min_val
        self.num_atoms = num_atoms
        self.min_val = min_val
        self.max_val = max_val
        self._delta = (self.max_val - self.min_val) / (self.num_atoms - 1)

    def atom_values(self):
        """Get the reward values for each atom."""
        return [self.min_val + i * self._delta for i in range(0, self.num_atoms)]

    def mean(self, log_probs):
        """Get the mean rewards for the distributions."""
        return tf.reduce_sum(tf.exp(log_probs) * tf.constant(self.atom_values), axis=-1)

    def add_rewards(self, log_probs, rewards, discounts):
        """
        Compute new distributions after adding rewards to
        old distributions.

        Args:
          log_probs: a batch of log probability vectors.
          rewards: a batch of rewards.
          discounts: the discount factors to apply to the
            distribution rewards.

        Returns:
          A new batch of log probability vectors.
        """
        minus_inf = tf.zeros_like(log_probs) - tf.constant(np.inf)
        new_probs = minus_inf
        for i, atom_rew in enumerate(self.atom_values()):
            old_probs = log_probs[:, i]
            # If the position is exactly 0, rounding up
            # and subtracting 1 would cause problems.
            new_idxs = ((rewards + discounts * atom_rew) - self.min_val) / self._delta
            new_idxs = tf.clip_by_value(new_idxs, 1e-5, float(self.num_atoms - 1))
            index1 = tf.cast(tf.ceil(new_idxs) - 1, tf.int32)
            frac1 = tf.abs(tf.ceil(new_idxs) - new_idxs)
            for indices, frac in [(index1, frac1), (index1 + 1, 1 - frac1)]:
                prob_offset = put_vector_elems(indices, old_probs - 1 + tf.log(frac),
                                               self.num_atoms)
                prob_offset = tf.where(tf.equal(prob_offset, 0), minus_inf, prob_offset + 1)
                new_probs = _add_log_probs(new_probs, prob_offset)
        return new_probs

def _add_log_probs(probs1, probs2):
    return tf.reduce_logsumexp(tf.stack([probs1, probs2]), axis=0)

def _kl_divergence(dists1, dists2):
    return tf.reduce_sum(tf.exp(dists1) * (dists1 - dists2), axis=-1)
