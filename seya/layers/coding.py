import theano
import theano.tensor as T
import numpy as np

from theano.tensor.shared_randomstreams import RandomStreams
srng = RandomStreams(seed=234)

from theano.tensor.signal.downsample import max_pool_2d
from keras.layers.core import Layer
from keras.layers.recurrent import Recurrent
from keras import activations, initializations
from keras.utils.theano_utils import alloc_zeros_matrix, sharedX

from ..utils import diff_abs

floatX = theano.config.floatX


def _proxOp(x, t):
    return T.maximum(x-t, 0) + T.minimum(x+t, 0)


def _proxInnov(x, x_tm1):
    innov = x - x_tm1
    i0 = T.maximum(innov, 1)
    i1 = T.minimum(i0, -1)
    return i1


def _IstaStep(cost, states, lr=.001, lambdav=.1, prior=0):
    grads = T.grad(cost, states)
    new_x = states-lr*grads
    if prior != 0:
        new_x += lambdav*lr*.1*_proxInnov(states, prior)
    new_states = _proxOp(states-lr*grads, lr*lambdav)
    return theano.gradient.disconnected_grad(new_states)


def _RMSPropStep(cost, states, accum_1, accum_2):
    rho = .9
    lr = .009
    momentum = .9
    epsilon = 1e-8

    grads = T.grad(cost, states)

    new_accum_1 = rho * accum_1 + (1 - rho) * grads**2
    new_accum_2 = momentum * accum_2 - lr * grads / T.sqrt(new_accum_1 + epsilon)
    denominator = T.sqrt(new_accum_1 + epsilon)
    new_states = states + momentum * new_accum_2 - lr * (grads / denominator)
    new_states = _proxOp(states - lr * (grads / denominator),
                         .1*lr/denominator) + momentum * new_accum_2
    return (theano.gradient.disconnected_grad(new_states),
            new_accum_1, new_accum_2)


class SparseCoding(Layer):
    def __init__(self, input_dim, output_dim,
                 init='glorot_uniform',
                 activation='linear',
                 truncate_gradient=-1,
                 gamma=.1,
                 n_steps=10,
                 return_reconstruction=False,
                 W_regularizer=None,
                 activity_regularizer=None):

        super(SparseCoding, self).__init__()
        self.init = initializations.get(init)
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.gamma = gamma
        self.n_steps = n_steps
        self.truncate_gradient = truncate_gradient
        self.activation = activations.get(activation)
        self.return_reconstruction = return_reconstruction
        self.input = T.matrix()

        self.W = self.init((self.output_dim, self.input_dim))
        self.params = [self.W, ]

        self.regularizers = []
        if W_regularizer:
            W_regularizer.set_param(self.W)
            self.regularizers.append(W_regularizer)
        if activity_regularizer:
            activity_regularizer.set_layer(self)
            self.regularizers.append(activity_regularizer)

    def get_initial_states(self, X):
        return alloc_zeros_matrix(X.shape[0], self.output_dim)

    def _step(self, x_t, inputs, prior, W):
        outputs = self.activation(T.dot(x_t, self.W))
        rec_error = T.sqr(inputs - outputs).sum()
        x = _IstaStep(rec_error, x_t, lambdav=self.gamma, prior=prior)
        return x, outputs

    def _get_output(self, inputs, train=False, prior=0):
        initial_states = self.get_initial_states(inputs)
        outputs, updates = theano.scan(
            self._step,
            sequences=[],
            outputs_info=[initial_states, None],
            non_sequences=[inputs, prior, self.W],
            n_steps=self.n_steps,
            truncate_gradient=self.truncate_gradient)

        outs = outputs[0][-1]
        if self.return_reconstruction:
            return T.dot(outs, self.W)
        else:
            return outs

    def get_output(self, train=False):
        inputs = self.get_input(train)
        return self._get_output(inputs, train)

    def get_config(self):
        return {"name": self.__class__.__name__,
                "input_dim": self.input_dim,
                "output_dim": self.output_dim,
                "init": self.init.__name__,
                "activation": self.activation.__name__,
                "truncate_gradient": self.truncate_gradient,
                "return_reconstruction": self.return_reconstruction}


class VarianceCoding(Layer):
    def __init__(self, input_dim, output_dim,
                 init='uniform',
                 truncate_gradient=-1,
                 gamma=.1,
                 n_steps=10,
                 W_regularizer=None,
                 activity_regularizer=None):

        super(VarianceCoding, self).__init__()
        self.init = initializations.get(init)
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.gamma = gamma
        self.n_steps = n_steps
        self.truncate_gradient = truncate_gradient
        self.activation = lambda x: .5*(1 + T.exp(-x))
        self.input = T.matrix()

        self.W = self.init((self.output_dim, self.input_dim))
        self.params = [self.W]

        self.regularizers = []
        if W_regularizer:
            W_regularizer.set_param(self.W)
            self.regularizers.append(W_regularizer)
        if activity_regularizer:
            activity_regularizer.set_layer(self)
            self.regularizers.append(activity_regularizer)

    def get_initial_states(self, X):
        return alloc_zeros_matrix(X.shape[0], self.output_dim)

    def _step(self, x_t, accum_1, accum_2, inputs, prior):
        outputs = self.activation(T.dot(x_t, self.W))
        l1_norm = self.gamma * outputs * diff_abs(inputs)
        own_norm = (diff_abs(x_t)).sum() * self.gamma
        l1_inov = T.sqr(x_t - prior).sum() * self.gamma / 10.
        cost = l1_norm.sum() + l1_inov + own_norm
        x, new_accum_1, new_accum_2 = _RMSPropStep(cost, x_t, accum_1, accum_2)
        return x, new_accum_1, new_accum_2, outputs, l1_norm

    def _get_output(self, inputs, train=False, prior=0):
        initial_states = self.get_initial_states(inputs)
        outputs, updates = theano.scan(
            self._step,
            sequences=[],
            outputs_info=[initial_states, ]*3 + [None, None],
            non_sequences=[inputs, prior] + self.params,
            n_steps=self.n_steps,
            truncate_gradient=self.truncate_gradient)
        return outputs[-1][-1]

    def get_output(self, train=False):
        inputs = self.get_input(train)
        return self._get_output(inputs, train)

    def get_config(self):
        return {"name": self.__class__.__name__,
                "input_dim": self.input_dim,
                "output_dim": self.output_dim,
                "init": self.init.__name__,
                "activation": self.activation.__name__,
                "truncate_gradient": self.truncate_gradient,
                "return_reconstruction": self.return_reconstruction}


class Sparse2L(Layer):
    '''A combined Sparse Coding + Variance Compoenent Layer
    '''
    def __init__(self, input_dim, output_dim, causes_dim,
                 init='glorot_uniform',
                 activation='linear',
                 truncate_gradient=-1,
                 gamma=.1,
                 n_steps=10,
                 return_mode='all',
                 W_regularizer=None,
                 V_regularizer=None,
                 activity_regularizer=None,
                 code_shape=None,
                 pool_size=None):

        super(Sparse2L, self).__init__()
        self.init = initializations.get(init)
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.causes_dim = causes_dim
        self.gamma = gamma
        self.n_steps = n_steps
        self.truncate_gradient = truncate_gradient
        self.activation = activations.get(activation)
        self.return_mode = return_mode
        self.input = T.matrix()

        self.pool_flag = False
        if (code_shape is not None) and (pool_size is not None):
            self.code_shape = code_shape
            self.pool_size = pool_size
            self.pool_flag = True

        self.W = self.init((self.output_dim, self.input_dim))
        if self.pool_flag:
            new_dim = int(np.sqrt(output_dim)/self.pool_size)**2
            self.V = sharedX(np.random.uniform(low=0, high=1,
                                               size=(self.causes_dim, new_dim)))

        else:
            self.V = sharedX(np.random.uniform(low=0, high=.1,
                                               size=(self.causes_dim,
                                                     self.output_dim)))
        self.params = [self.W, self.V]

        self.regularizers = []
        if W_regularizer:
            W_regularizer.set_param(self.W)
            self.regularizers.append(W_regularizer)
            V_regularizer.set_param(self.V)
            self.regularizers.append(V_regularizer)
        if activity_regularizer:
            activity_regularizer.set_layer(self)
            self.regularizers.append(activity_regularizer)

    def get_initial_states(self, inputs):
        u_init = alloc_zeros_matrix(inputs.shape[0], self.causes_dim) + .1
        return (alloc_zeros_matrix(inputs.shape[0], self.output_dim), u_init)

    def _step(self, x_tm1, u_tm1, inputs, prior, *args):
        outputs = self.activation(T.dot(x_tm1, self.W))
        rec_error = T.sqr(inputs - outputs).sum()
        causes = (1 + T.exp(-T.dot(u_tm1, self.V))) * .5

        if self.pool_flag:
            batch_size = inputs.shape[0]
            dim = causes.shape[1]
            imgs = T.cast(T.sqrt(dim), 'int64')
            causes_up = causes.reshape(
                (batch_size, 1, imgs, imgs)).repeat(
                    self.pool_size, axis=2).repeat(self.pool_size,
                                                   axis=3).flatten(ndim=2)
        else:
            causes_up = causes

        x = _IstaStep(rec_error, x_tm1, lambdav=self.gamma*causes_up,
                      prior=prior)

        if self.pool_flag:
            dim = T.cast(T.sqrt(x.shape[1]), 'int64')
            x_pool = x.reshape((batch_size, 1, dim, dim))
            x_pool = max_pool_2d(x_pool, ds=(self.pool_size, )*2).flatten(ndim=2)
        else:
            x_pool = x

        u_cost = causes * x_pool * self.gamma
        u = _IstaStep(u_cost.sum(), u_tm1, lambdav=self.gamma/10.)
        causes = (1 + T.exp(-T.dot(u, self.V))) * .5
        u_cost = causes * x_pool * self.gamma

        return (x, u, u_cost, outputs)

    def _get_output(self, inputs, prior=0, train=False):
        x_init, u_init = self.get_initial_states(inputs)
        outputs, updates = theano.scan(
            self._step,
            sequences=[],
            outputs_info=[x_init, u_init, None, None],
            non_sequences=[inputs, prior] + self.params,
            n_steps=self.n_steps,
            truncate_gradient=self.truncate_gradient)

        if self.return_mode == 'rec':
            return outputs[3][-1]
        elif self.return_mode == 'states':
            return outputs[0][-1]
        elif self.return_mode == 'causes':
            return outputs[1][-1]
        elif self.return_mode == 'all':
            return [outputs[0][-1],
                    outputs[1][-1],
                    outputs[2][-1].sum(axis=-1, keepdims=True),
                    outputs[3][-1]]

        else:
            raise ValueError

    def get_output(self, train=False):
        inputs = self.get_input(train)
        return self._get_output(inputs, train=train)

    def get_config(self):
        return {"name": self.__class__.__name__,
                "input_dim": self.input_dim,
                "output_dim": self.output_dim,
                "init": self.init.__name__,
                "activation": self.activation.__name__,
                "truncate_gradient": self.truncate_gradient,
                "return_reconstruction": self.return_reconstruction}


class ConvSparseCoding(Layer):
    def __init__(self, nb_filter, stack_size, nb_row, nb_col,
                 input_row, input_col,
                 init='glorot_uniform', activation='linear', weights=None,
                 border_mode='valid', subsample=(1, 1),
                 W_regularizer=None,
                 activity_regularizer=None,
                 return_reconstruction=False, n_steps=10, truncate_gradient=-1,
                 gamma=0.1):

        super(ConvSparseCoding, self).__init__()
        self.init = initializations.get(init)
        self.activation = activations.get(activation)
        self.subsample = subsample
        self.border_mode = border_mode
        self.nb_filter = nb_filter
        self.stack_size = stack_size
        self.n_steps = n_steps
        self.truncate_gradient = truncate_gradient
        self.gamma = gamma

        self.nb_row = nb_row
        self.nb_col = nb_col
        if border_mode == 'valid':
            self.code_row = input_row + nb_row - 1
            self.code_col = input_col + nb_col - 1
        else:
            raise ValueError("boder_model {0} not implemented yet.".format(
                border_mode))

        self.input = T.tensor4()
        self.W_shape = (nb_filter, stack_size, nb_row, nb_col)
        self.W = self.init(self.W_shape)

        self.params = [self.W]

        self.regularizers = []
        if W_regularizer:
            W_regularizer.set_param(self.W)
            self.regularizers.append(W_regularizer)

        if weights is not None:
            self.set_weights(weights)

        self.return_reconstruction = return_reconstruction

    def get_initial_states(self, X):
        return alloc_zeros_matrix(X.shape[0], self.stack_size,
                                  self.code_row, self.code_col)

    def _step(self, x_t, inputs, prior, *args):
        conv_out = T.nnet.conv.conv2d(x_t, self.W, border_mode=self.border_mode,
                                      subsample=self.subsample)
        outputs = self.activation(conv_out)
        rec_error = T.sqr(inputs - outputs).sum()
        x = _IstaStep(rec_error, x_t, lambdav=self.gamma, prior=prior)
        return x, outputs

    def _get_output(self, inputs, train):
        initial_states = self.get_initial_states(inputs)
        prior = 0
        outputs, updates = theano.scan(
            self._step,
            sequences=[],
            outputs_info=[initial_states, None],
            non_sequences=[inputs, prior] + self.params,
            n_steps=self.n_steps,
            truncate_gradient=self.truncate_gradient)

        if self.return_reconstruction:
            return outputs[-1][-1]
        else:
            return outputs[0][-1]

    def get_output(self, train):
        inputs = self.get_input(train)
        return self._get_output(inputs, train)


class ConvSparse2L(Layer):
    def __init__(self, nb_filter, stack_size, nb_row, nb_col,
                 input_row, input_col,
                 init='glorot_uniform', activation='linear', weights=None,
                 border_mode='valid', subsample=(1, 1),
                 W_regularizer=None,
                 V_regularizer=None,
                 activity_regularizer=None,
                 return_reconstruction=False, n_steps=10, truncate_gradient=-1,
                 gamma=0.1):

        super(ConvSparseCoding, self).__init__()
        self.init = initializations.get(init)
        self.activation = activations.get(activation)
        self.subsample = subsample
        self.border_mode = border_mode
        self.nb_filter = nb_filter
        self.stack_size = stack_size
        self.n_steps = n_steps
        self.truncate_gradient = truncate_gradient
        self.gamma = gamma

        self.nb_row = nb_row
        self.nb_col = nb_col
        if border_mode == 'valid':
            self.code_row = input_row + nb_row - 1
            self.code_col = input_col + nb_col - 1
        else:
            raise ValueError("boder_model {0} not implemented yet.".format(
                border_mode))

        self.input = T.tensor4()
        self.W_shape = (nb_filter, stack_size, nb_row, nb_col)
        self.W = self.init(self.W_shape)

        self.params = [self.W]

        self.regularizers = []
        if W_regularizer:
            W_regularizer.set_param(self.W)
            self.regularizers.append(W_regularizer)

        if weights is not None:
            self.set_weights(weights)

        self.return_reconstruction = return_reconstruction

    def get_initial_states(self, X):
        return alloc_zeros_matrix(X.shape[0], self.stack_size,
                                  self.code_row, self.code_col)

    def _step(self, x_t, accum_1, accum_2, inputs):
        conv_out = T.nnet.conv.conv2d(x_t, self.W, border_mode=self.border_mode,
                                      subsample=self.subsample)
        outputs = self.activation(conv_out)
        rec_error = T.sqr(inputs - outputs).sum()
        x, new_accum_1, new_accum_2 = _IstaStep(rec_error, x_t, lambdav=self.gamma)
        return x, outputs

    def _get_output(self, inputs, train):
        initial_states = self.get_initial_states(inputs)
        outputs, updates = theano.scan(
            self._step,
            sequences=[],
            outputs_info=[initial_states, None],
            non_sequences=inputs,
            n_steps=self.n_steps,
            truncate_gradient=self.truncate_gradient)

        if self.return_reconstruction:
            return outputs[-1][-1]
        else:
            return outputs[0][-1]

    def get_output(self, train):
        inputs = self.get_input(train)
        return self._get_output(inputs, train)


class TSC(Recurrent):
    def __init__(self, s2l, truncate_gradient=1,
                 return_mode='all',
                 init='glorot_uniform'):
        super(TSC, self).__init__()
        self.return_sequences = True
        self.truncate_gradient = truncate_gradient
        self.init = initializations.get(init)
        s2l.return_mode = return_mode
        self.s2l = s2l
        self.A = self.init((s2l.output_dim, s2l.output_dim))
        self.params = s2l.params + [self.A, ]
        self.input = T.tensor3()

    def _step(self, inp, x_t, u_t, *args):
        x_prior = T.dot(x_t, self.A)
        x, u, u_cost, outputs = self.s2l._get_output(inputs=inp, prior=x_prior,
                                                     train=False)
        return x, u, u_cost, outputs

    def get_output(self, train=False):
        X = self.get_input().dimshuffle(1, 0, 2)
        outputs, updates = theano.scan(
            self._step,
            sequences=X,
            outputs_info=self.s2l.get_initial_states(X[0])+(None, None),
            non_sequences=self.params,
            truncate_gradient=self.truncate_gradient)
        outputs = [o.dimshuffle(1, 0, 2) for o in outputs]
        return outputs


class TemporalSparseCoding(Recurrent):
    def __init__(self, prototype, transition_net, truncate_gradient=-1,
                 return_mode='reconstruction',
                 init='glorot_uniform'):

        super(TemporalSparseCoding, self).__init__()
        self.return_sequences = True
        self.init = initializations.get(init)
        self.prototype = prototype
        self.W = prototype.W  # Sparse coding parameter I - Wx
        self.regularizers = prototype.regularizers
        self.activation = prototype.activation
        self.tnet = transition_net
        try:
            self.is_conv = False
            self.input_dim = prototype.input_dim
            self.output_dim = prototype.output_dim
            self.A = self.init((
                self.output_dim, self.output_dim))  # Predictive transition x_t - Ax_t-1
            self.input = T.tensor3()
        except:
            self.is_conv = True
            self.nb_filter = prototype.nb_filter
            self.stack_size = prototype.stack_size
            self.nb_row = prototype.nb_row
            self.nb_col = prototype.nb_col
            self.A = self.init(self.W.get_value().shape)
            self.input = T.TensorType(floatX, (False,)*5)()

        self.params = prototype.params  # + [self.A, ]
        self.truncate_gradient = truncate_gradient
        self.return_mode = return_mode

    def _step(self, inputs, x_t, u_t, *args):
        tmp = self.tnet.input
        self.tnet.input = x_t
        prior = self.tnet.get_output()
        self.tnet.input = tmp
        if self.is_conv:
            new_x, new_u, u_cost, rec = self.prototype._get_output(inputs=inputs,
                                                                   prior=prior)
            inp = T.nnet.conv.conv2d(new_x, self.W, border_mode=self.border_mode,
                                     subsample=self.subsample)
            outputs = self.activation(inp) + inputs.sum() * 0
        else:
            new_x, new_u, u_cost, rec = self.prototype._get_output(inputs=inputs,
                                                                   prior=prior)
            outputs = self.activation(T.dot(new_x, self.W)) + inputs.sum() * 0
        return new_x, new_u, u_cost, outputs

    def get_output(self, train=False):
        inputs = self.get_input(train).dimshuffle(1, 0, 2)
        initial_states = self.prototype.get_initial_states(inputs[0])
        outputs, updates = theano.scan(
            self._step,
            sequences=[inputs],
            outputs_info=initial_states + (None, None),
            non_sequences=self.params + self.tnet.params,
            truncate_gradient=self.truncate_gradient)

        if self.return_mode == 'reconstruction':
            return outputs[-1].dimshuffle(1, 0, 2)
        elif self.return_mode == 'all':
            return [outputs[0].dimshuffle(1, 0, 2) + inputs.sum() * 0,
                    outputs[1].dimshuffle(1, 0, 2) + inputs.sum() * 0,
                    outputs[2].dimshuffle(1, 0, 2) + inputs.sum() * 0,
                    outputs[3].dimshuffle(1, 0, 2) + inputs.sum() * 0]

    def get_config(self):
        return {"name": self.__class__.__name__,
                # "input_dim": self.input_dim,
                # "output_dim": self.output_dim,
                "init": self.init.__name__,
                "activation": self.activation.__name__,
                "truncate_gradient": self.truncate_gradient,
                "return_mode": self.return_mode}
