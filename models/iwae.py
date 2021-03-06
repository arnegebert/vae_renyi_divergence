import numpy as np
import tensorflow as tf
import time
from scipy.misc import logsumexp
from network.network import construct_network
from loss_functions import reconstruction_loss
from loss_functions import log_prior
from vae import variational_lowerbound

def iwae(x, encoder, decoder, num_samples, batch_size, alpha = 0.0):
    """
    Compute the loss function of VR lowerbound
    """
    #logpxz, logqzx, z_list = reconstruction_loss(x, encoder, decoder, num_samples)
    logp_x_given_z = 0.0
    logq_z_given_x = 0.0
    L = len(encoder.S_layers)
    # (N*K, F)
    x_rep = tf.tile(x, [num_samples, 1]) 
    input = x_rep

    # do encoding
    samples = []
    for l in xrange(L):
        output, logq = encoder.S_layers[l].encode_and_log_prob(input)
        # summing over these since latents are all independent given their previous one
        logq_z_given_x = logq_z_given_x + logq
        samples.append(output)
        input = output
    # at this point logq_z_given_x is q(z|x)
    # dimension of q(z|x) is (N*K)
    # do decoding
    samples = list(reversed(samples))
    samples.append(x_rep)
    for l in xrange(L):
        _, logp = decoder.S_layers[l].encode_and_log_prob(samples[l], eval_output = samples[l+1])
        logp_x_given_z = logp_x_given_z + logp
    # same reasoning, logp_x_given_z is p(x|z) (N*K)

    # get probability type of last encoder layer to compute p(z_l)
    logpz = log_prior(output, encoder.S_layers[l].get_prob_type())

    # (N*K)
    logF = logpz + logp_x_given_z - logq_z_given_x
    
    # first compute lowerbound
    K = float(num_samples)
    # (K, N), unnormalized importance weight but for alpha-divergence
    logF_matrix = tf.reshape(logF, [num_samples, batch_size]) * (1 - alpha)
    # (N), choose sample with highest weight
    logF_max = tf.reduce_max(logF_matrix, 0)
    # normalize it? subtract max weight from each weight
    logF_matrix -= logF_max
    # sum over all samples for each datapoint and make them positive
    # (K, N) -> (N)
    logF_normalizer = tf.clip_by_value(tf.reduce_sum(tf.exp(logF_matrix), 0), 1e-9, np.inf)
    # (N)
    logF_normalizer = tf.log(logF_normalizer)
    # note here we need to substract log K as we use reduce_sum above
    if np.abs(alpha - 1.0) > 10e-3:
        # alpha > 1?
        # (N) -> (1)

        # what theyre definitely computing here is
        # 1/N 1/(1-alpha) sum_minibatch * log 1/K sum_samples w_i_alpha
        # MC approx equation (8), IWAE paper
        lowerbound = tf.reduce_mean(logF_normalizer + logF_max - tf.log(K)) / (1 - alpha)
    else:
        # compute mean over all samples over all data points
        # just sum over log p... for all data points in the mini batch
        # this is analogue to doing a monte carlo approximation of the ELBO in the standard case
        # youre just doing this here summing here, since this is pretty much vanilla IWAE
        # BUT youre using an alpha-divergence as divergence instead
        # (N*K) -> (1)
        lowerbound = tf.reduce_mean(logF)
    
    # now compute the importance weighted version of gradients
    # subtract normalized weights from all samples for each data point
    # then reshape to K*N
    log_ws = tf.reshape(logF_matrix - logF_normalizer, shape=[-1])
    ws = tf.stop_gradient(tf.exp(log_ws), name = 'importance_weights_no_grad')
    params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)
    # what theyre definitely computing here is
    # sum_samples (w_norm_alpha_k * log w_i)
    gradients = tf.gradients(-logF * ws, params)
    grad = zip(gradients, params)

    return lowerbound, grad
    
def make_functions_vae(models, input_size, num_samples, batch_size, alpha = 0.0): 
    encoder, decoder = models  
 
    input = tf.placeholder(tf.float32, [batch_size, input_size])
    lowerbound, grad = iwae(input, encoder, decoder, num_samples, batch_size, \
                                        alpha)
                                        
    learning_rate_ph = tf.placeholder(tf.float32, shape = [])
    optimizer = \
            tf.train.AdamOptimizer(learning_rate=learning_rate_ph, \
                                   beta1=0.9, beta2=0.999, epsilon=10e-8 \
                                   ).apply_gradients(grad)
    
    def updateParams(sess, X, learning_rate = 0.0005):
        opt, cost = sess.run((optimizer, lowerbound),
                           feed_dict={input: X,
                                      learning_rate_ph:learning_rate})
        return cost

    return updateParams, lowerbound                               
                                        
def init_optimizer(models, input_size, batch_size = 100, num_samples = 1, **kwargs):
    
    encoder = models[0]; decoder = models[1]
    # vae
    if 'alpha' not in kwargs:
        alpha = 0.0
    else:
        alpha = kwargs['alpha']
    updateParams, lowerbound = \
        make_functions_vae(models, input_size, \
                           num_samples, batch_size, \
                           alpha)

    def fit(sess, X, n_iter = 100, learning_rate = 0.0005, verbose = True):
        # first make batches of source data
        [N, dimX] = X.shape        
        N_batch = N / batch_size
        if np.mod(N, batch_size) != 0:
            N_batch += 1      
        print "training the model for %d iterations with lr=%f" % \
            (n_iter, learning_rate)

        begin = time.time()
        for iteration in xrange(1, n_iter + 1):
            iteration_lowerbound = 0
            ind_s = np.random.permutation(range(N))

            for j in xrange(0, N_batch):
                indl = j * batch_size
                indr = (j+1) * batch_size
                ind = ind_s[indl:min(indr, N)]
                if indr > N:
                    ind = np.concatenate((ind, ind_s[:(indr-N)]))
                batch = X[ind]
                lowerbound = updateParams(sess, batch, learning_rate)
                iteration_lowerbound += lowerbound * batch_size

            if verbose:
                end = time.time()
                print("Iteration %d, lowerbound = %.2f, time = %.2fs"
                      % (iteration, iteration_lowerbound / N, end - begin)) 
                begin = end
                
        
    def eval_test_ll(sess, X, num_samples):
        lowerbound = sess.run(variational_lowerbound(X, encoder, decoder, num_samples, X.shape[0], 0.0))
        
        return lowerbound

    def score(sess, X, num_samples = 100):
        """
        Computer lower bound on data, following the IWAE paper.
        """
        
        begin = time.time()
        print 'num. samples for eval:', num_samples
        
        # compute log_q
        lowerbound_total = 0
        num_data_test = X.shape[0]
        if num_data_test % batch_size == 0:
            num_batch = num_data_test / batch_size
        else:
            num_batch = num_data_test / batch_size + 1
        
        for i in xrange(num_batch):
            indl = i*batch_size
            indr = min((i+1)*batch_size, num_data_test)
            minibatch = X[indl:indr]
            lowerbound = eval_test_ll(sess, minibatch, num_samples)
            lowerbound_total += lowerbound * (indr - indl)
        
        end = time.time()
        time_test = end - begin
        lowerbound_total = lowerbound_total / float(num_data_test)

        return lowerbound_total, time_test
     
    return fit, score                              
