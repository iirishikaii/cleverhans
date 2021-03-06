"""The CarliniWagnerL2 attack
"""
# pylint: disable=missing-docstring
import logging

import numpy as np
import tensorflow as tf

from cleverhans.attacks.attack_ae import Attack
from cleverhans.compat import reduce_sum, reduce_max
from cleverhans.model import CallableModelWrapper, Model, wrapper_warning_logits
from cleverhans import utils
import tensorflow.contrib.slim as slim  

np_dtype = np.dtype('float32')
tf_dtype = tf.as_dtype('float32')

_logger = utils.create_logger("cleverhans.attacks.carlini_wagner_l2")
_logger.setLevel(logging.INFO)


class CarliniWagnerAE_Lat_Keras(Attack):
 
  def __init__(self, model, cl_model, sess, dtypestr='float32', **kwargs):
    """
    Note: the model parameter should be an instance of the
    cleverhans.model.Model abstraction provided by CleverHans.
    """
    if not isinstance(cl_model, Model):
      wrapper_warning_logits()
      cl_model = CallableModelWrapper(cl_model, 'logits')
    if not isinstance(model, Model):
      wrapper_warning_logits()
      model = CallableModelWrapper(model, 'logits')

    super(CarliniWagnerAE_Lat_Keras, self).__init__(model, sess, dtypestr, **kwargs)
    self.cl_model = cl_model
    self.feedable_kwargs = ('y', 'y_target')

    self.structural_kwargs = [
        'batch_size', 'confidence', 'targeted', 'learning_rate',
        'binary_search_steps', 'max_iterations', 'abort_early',
        'initial_const', 'clip_min', 'clip_max'
    ]

  def generate(self, x,x_t, **kwargs):
    
    assert self.sess is not None, \
        'Cannot use `generate` when no `sess` was provided'
    self.parse_params(**kwargs)

    #labels, nb_classes = self.get_or_guess_labels(x, kwargs)
    nb_classes = 10
    attack = CWL2(self.sess, self.model, self.cl_model, self.batch_size, self.confidence,
                  'x_target' in kwargs, self.learning_rate,
                  self.binary_search_steps, self.max_iterations,
                  self.abort_early, self.initial_const, self.clip_min,
                  self.clip_max, nb_classes,
                  x.get_shape().as_list()[1:])

    def cw_wrap(x_val, x_targ_val):
      return np.array(attack.attack(x_val, x_targ_val), dtype=self.np_dtype)

    wrap = tf.py_func(cw_wrap, [x, x_t], self.tf_dtype)
    wrap.set_shape(x.get_shape())

    return wrap

  def parse_params(self,
                   y=None,
                   y_target = None,
                   x_target=None,
                   batch_size=1,
                   confidence=0,
                   learning_rate=5e-2,
                   binary_search_steps=10,
                   max_iterations=1000,
                   abort_early=False,
                   initial_const=0.5,
                   clip_min=0,
                   clip_max=1):
    

    # ignore the y and y_target argument
    self.batch_size = batch_size
    self.confidence = confidence
    self.learning_rate = learning_rate
    self.binary_search_steps = binary_search_steps
    self.max_iterations = max_iterations
    self.abort_early = abort_early
    self.initial_const = initial_const
    self.clip_min = clip_min
    self.clip_max = clip_max


def ZERO():
  return np.asarray(0., dtype=np_dtype)


class CWL2(object):
  def __init__(self, sess, model,cl_model, batch_size, confidence, targeted,
               learning_rate, binary_search_steps, max_iterations,
               abort_early, initial_const, clip_min, clip_max, num_labels,
               shape):
    
    self.sess = sess
    self.TARGETED = targeted
    self.LEARNING_RATE = learning_rate
    self.MAX_ITERATIONS = max_iterations
    self.BINARY_SEARCH_STEPS = binary_search_steps
    self.ABORT_EARLY = abort_early
    self.CONFIDENCE = confidence
    self.initial_const = initial_const
    self.batch_size = batch_size
    self.clip_min = clip_min
    self.clip_max = clip_max
    self.model = model
    self.cl_model = cl_model

    latent_layer_model = Model(inputs=model.input,
                                 outputs=model.get_layer("latent").output)

    self.repeat = binary_search_steps >= 10

    self.shape = shape = tuple([batch_size] + list(shape))
    #print("shape: ", shape)

    # the variable we're going to optimize over
    modifier = tf.Variable(np.zeros(shape, dtype=np_dtype))

    # these are variables to be more efficient in sending data to tf
    self.timg = tf.Variable(np.zeros(shape), dtype=tf_dtype, name='timg')
    self.targimg = tf.Variable(np.zeros(shape), dtype=tf_dtype, name='targimg')
    #self.tlab = tf.Variable(
     #   np.zeros((batch_size, num_labels)), dtype=tf_dtype, name='tlab')
    self.const = tf.Variable(
        np.zeros(batch_size), dtype=tf_dtype, name='const')

    # and here's what we use to assign them
    self.assign_timg = tf.placeholder(tf_dtype, shape, name='assign_timg')
    self.assign_targimg = tf.placeholder(tf_dtype, shape, name='assign_targimg')
    #self.assign_tlab = tf.placeholder(
     #   tf_dtype, (batch_size, num_labels), name='assign_tlab')
    self.assign_const = tf.placeholder(
        tf_dtype, [batch_size], name='assign_const')

    # the resulting instance, tanh'd to keep bounded from clip_min
    # to clip_max
    self.newimg = (tf.tanh(modifier + self.timg) + 1) / 2
    self.newimg = self.newimg * (clip_max - clip_min) + clip_min

    targimg_lat = latent_layer_model.predict(self.targimg)
    
    
    self.x_hat = model.predict(self.newimg, steps = 1)
    self.x_hat_lat = latent_layer_model.predict(self.newimg)

    self.y_hat_logit = cl_model.prediction(self.x_hat_lat, steps = 1)
    self.y_hat = tf.argmax(self.y_hat_logit, axis = 1)

    
    self.y_targ_logit = cl_model.predict(targimg_lat, steps = 1)
    self.y_targ = tf.argmax(self.y_targ_logit, axis = 1)

    # distance to the input data
    self.other = (tf.tanh(self.timg) + 1) / 2
    self.other =  self.other * (clip_max - clip_min) + clip_min
    self.l2dist = reduce_sum(
        tf.square(self.newimg - self.other), list(range(1, len(shape))))

    print("shape of l2_dist: ", np.shape(self.l2dist))

    
    epsilon = 10e-8
    
    loss1 = reduce_sum(tf.square(self.x_hat_lat-targimg_lat))
    
    # sum up the losses
    self.loss2 = reduce_sum(self.l2dist)
    self.loss1 = reduce_sum(self.const * loss1)
    self.loss = self.loss1 + self.loss2

    # Setup the adam optimizer and keep track of variables we're creating
    start_vars = set(x.name for x in tf.global_variables())
    optimizer = tf.train.AdamOptimizer(self.LEARNING_RATE)
    self.train = optimizer.minimize(self.loss, var_list=[modifier])
    end_vars = tf.global_variables()
    new_vars = [x for x in end_vars if x.name not in start_vars]

    # these are the variables to initialize when we run
    self.setup = []
    self.setup.append(self.timg.assign(self.assign_timg))
    self.setup.append(self.targimg.assign(self.assign_targimg))
    #self.setup.append(self.tlab.assign(self.assign_tlab))
    self.setup.append(self.const.assign(self.assign_const))

    self.init = tf.variables_initializer(var_list=[modifier] + new_vars)

  def attack(self, imgs, targ_imgs):
    
    print("batch_size in attack: ", self.batch_size)
    r = []
    for i in range(0, len(imgs), self.batch_size):
      _logger.debug(
          ("Running CWL2 attack on instance %s of %s", i, len(imgs)))
      r.extend(
          self.attack_batch(imgs[i:i + self.batch_size],
                            targ_imgs[i:i + self.batch_size]))
    return np.array(r)

  def attack_batch(self, imgs, targ_imgs):
    """
    Run the attack on a batch of instance and labels.
    """

    def compare(y1,y2):
      if(y1==y2):
        return True
      else:
        return False

    def compare_dist(recon, orig, targ):
      
      a = np.sum((recon - orig)**2)
      b = np.sum((recon-targ)**2)
      #if  (tf.math.greater(a,b)) :
      if(a+80>b):
        return True
      else:
        return False
    
    batch_size = self.batch_size

    #print("batch_size: ", batch_size)

    oimgs = np.clip(imgs, self.clip_min, self.clip_max)
    #oimgs_lat = self.model.get_layer(oimgs, 'LATENT')

    # re-scale instances to be within range [0, 1]
    imgs = (imgs - self.clip_min) / (self.clip_max - self.clip_min)
    imgs = np.clip(imgs, 0, 1)
    # now convert to [-1, 1]
    imgs = (imgs * 2) - 1
    # convert to tanh-space
    imgs = np.arctanh(imgs * .999999)

    # set the lower and upper bounds accordingly
    lower_bound = np.zeros(batch_size)
    CONST = np.ones(batch_size) * self.initial_const
    upper_bound = np.ones(batch_size) * 1e8

    # placeholders for the best l2, score, and instance attack found so far
    o_bestl2 = [1e10] * batch_size
    #o_bestrec = np.copy(oimgs)
    o_bestrec = np.copy(oimgs)
    o_bestattack = np.copy(oimgs)

    for outer_step in range(self.BINARY_SEARCH_STEPS):
      # completely reset adam's internal state.
      self.sess.run(self.init)
      batch = imgs[:batch_size]
      batchtarg = targ_imgs[:batch_size]

      bestl2 = [1e10] * batch_size
      #bestrec = np.copy(oimgs)
      bestrec = np.copy(oimgs)

      _logger.debug("  Binary search step %s of %s",
                    outer_step, self.BINARY_SEARCH_STEPS)

      # The last iteration (if we run many steps) repeat the search once.
      if self.repeat and outer_step == self.BINARY_SEARCH_STEPS - 1:
        CONST = upper_bound

      # set the variables so that we don't have to send them over again
      self.sess.run(
          self.setup, {
              self.assign_timg: batch,
              self.assign_targimg: batchtarg,
              self.assign_const: CONST
          })

      prev = 1e8
      for iteration in range(self.MAX_ITERATIONS):
        # perform the attack
        _, l, l2s, nrec, nimg, yhat, ytarg = self.sess.run([
            self.train, self.loss, self.l2dist, self.x_hat,
            self.newimg, self.y_hat, self.y_targ
        ])
        #print("shape of yhat: ", np.shape(yhat))
        if iteration % ((self.MAX_ITERATIONS // 10) or 1) == 0:
          _logger.debug(("    Iteration {} of {}: loss={:.3g} " +
                         "l2={:.3g} ").format(
                             iteration, self.MAX_ITERATIONS, l,
                             np.mean(l2s)))

        # check if we should abort search if we're getting nowhere.
        if self.ABORT_EARLY and \
           iteration % ((self.MAX_ITERATIONS // 10) or 1) == 0:
          if l > prev * .9999:
            msg = "    Failed to make progress; stop early"
            _logger.debug(msg)
            break
          prev = l

        # adjust the best result found so far
        for e, (l2, nr, ii, yh, yt) in enumerate(zip(l2s, nrec, nimg, yhat, ytarg)):
          #lab = np.argmax(batchlab[e])
          if l2 < bestl2[e] and compare(yh,yt):
          #if l2<bestl2[e] and compare_dist(nr, imgs[e], targ_imgs[e]):
            bestl2[e] = l2
            bestrec[e] = nr
          if l2 < o_bestl2[e] and compare(yh,yt):
          #if l2 < o_bestl2[e] and compare_dist(nr, imgs[e], targ_imgs[e]):
            o_bestl2[e] = l2
            o_bestrec[e] = nr
            o_bestattack[e] = ii

      # adjust the constant as needed
      for e in range(batch_size):
        if compare(yhat[e], ytarg[e]):
        #if compare_dist(nrec[e], imgs[e], targ_imgs[e]):
          upper_bound[e] = min(upper_bound[e], CONST[e])
          if upper_bound[e] < 1e9:
            CONST[e] = (lower_bound[e] + upper_bound[e]) / 2
        else:
          # failure, either multiply by 10 if no solution found yet
          #          or do binary search with the known upper bound
          lower_bound[e] = max(lower_bound[e], CONST[e])
          if upper_bound[e] < 1e9:
            CONST[e] = (lower_bound[e] + upper_bound[e]) / 2
          else:
            CONST[e] *= 10
      _logger.debug("  Successfully generated adversarial examples " +
                    "on {} of {} instances.".format(
                        sum(upper_bound < 1e9), batch_size))
      o_bestl2 = np.array(o_bestl2)
      mean = np.mean(np.sqrt(o_bestl2[o_bestl2 < 1e9]))
      _logger.debug("   Mean successful distortion: {:.4g}".format(mean))

    # return the best solution found
    o_bestl2 = np.array(o_bestl2)
    #print("o_bestl2: ", o_bestl2)
    #print("shape of o_bestattack: ", np.shape(o_bestattack))
    return o_bestattack
