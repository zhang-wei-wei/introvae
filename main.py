import numpy as np
import tensorflow as tf
import keras, keras.backend as K

from keras.layers import Input
from keras.models import Model

import os, sys, time
from collections import OrderedDict

import model, params, losses, utils, data

#
# Config
#

args = params.getArgs()
print(args)

# set random seed
np.random.seed(10)

print('Keras version: ', keras.__version__)
print('Tensorflow version: ', tf.__version__)
from keras.backend.tensorflow_backend import set_session
config = tf.ConfigProto()
config.gpu_options.per_process_gpu_memory_fraction = args.memory_share
set_session(tf.Session(config=config))

#
# Datasets
#

K.set_image_data_format('channels_first')

data_path = os.path.join(args.datasets_dir, args.dataset)

iterations = args.nb_epoch * args.train_size // args.batch_size
iterations_per_epoch = args.train_size // args.batch_size

train_dataset, train_iterator, train_iterator_init_op, train_next \
     = data.create_dataset(os.path.join(data_path, "train/*.npy"), args.batch_size, args.train_size)
test_dataset, test_iterator, test_iterator_init_op, test_next \
     = data.create_dataset(os.path.join(data_path, "test/*.npy"), args.batch_size, args.test_size)
fixed_dataset, fixed_iterator, fixed_iterator_init_op, fixed_next \
     = data.create_dataset(os.path.join(data_path, "train/*.npy"), args.batch_size, args.latent_cloud_size)

args.n_channels = 3 if args.color else 1
args.original_shape = (args.n_channels, ) + args.shape

#
# Build networks
#

encoder_layers = model.encoder_layers_introvae(args.shape, args.base_filter_num, args.encoder_use_bn)
generator_layers = model.generator_layers_introvae(args.shape, args.base_filter_num, args.generator_use_bn)

encoder_input = Input(batch_shape=[args.batch_size] + list(args.original_shape), name='encoder_input')
generator_input = Input(batch_shape=(args.batch_size, args.latent_dim), name='generator_input')

encoder_output = encoder_input
for layer in encoder_layers:
    encoder_output = layer(encoder_output)

generator_output = generator_input
for layer in generator_layers:
    generator_output = layer(generator_output)

z, z_mean, z_log_var = model.add_sampling(encoder_output, args.sampling, args.sampling_std, args.batch_size, args.latent_dim, args.encoder_wd)

encoder = Model(inputs=encoder_input, outputs=[z_mean, z_log_var])
generator = Model(inputs=generator_input, outputs=generator_output)

xr = generator(z)
reconst_latent_input = Input(batch_shape=(args.batch_size, args.latent_dim), name='reconst_latent_input')
zr_mean, zr_log_var = encoder(generator(reconst_latent_input))
zr_mean_ng, zr_log_var_ng = encoder(tf.stop_gradient(generator(reconst_latent_input)))
xr_latent = generator(reconst_latent_input)

sampled_latent_input = Input(batch_shape=(args.batch_size, args.latent_dim), name='sampled_latent_input')
zpp_mean, zpp_log_var = encoder(generator(sampled_latent_input))
zpp_mean_ng, zpp_log_var_ng = encoder(tf.stop_gradient(generator(sampled_latent_input)))

encoder_optimizer = tf.train.AdamOptimizer(learning_rate=args.lr)
generator_optimizer = tf.train.AdamOptimizer(learning_rate=args.lr)

print('Encoder')
encoder.summary()
print('Generator')
generator.summary()

#
# Define losses
#

l_reg_z = losses.reg_loss(z_mean, z_log_var)
l_reg_zr_ng = losses.reg_loss(zr_mean_ng, zr_log_var_ng)
l_reg_zpp_ng = losses.reg_loss(zpp_mean_ng, zpp_log_var_ng)

l_ae = losses.mse_loss(encoder_input, xr, args.original_shape)
l_ae2 = losses.mse_loss(encoder_input, xr_latent, args.original_shape)

encoder_l_adv = l_reg_z + args.alpha * K.maximum(0., args.m - l_reg_zr_ng) + args.alpha * K.maximum(0., args.m - l_reg_zpp_ng)
encoder_loss = encoder_l_adv + args.beta * l_ae

l_reg_zr = losses.reg_loss(zr_mean, zr_log_var)
l_reg_zpp = losses.reg_loss(zpp_mean, zpp_log_var)

generator_l_adv = args.alpha * l_reg_zr + args.alpha * l_reg_zpp
generator_loss = generator_l_adv + args.beta * l_ae2

#
# Define training step operations
#

encoder_params = encoder.trainable_weights
generator_params = generator.trainable_weights

encoder_grads = encoder_optimizer.compute_gradients(encoder_loss, var_list=encoder_params)
encoder_apply_grads_op = encoder_optimizer.apply_gradients(encoder_grads)

generator_grads = generator_optimizer.compute_gradients(generator_loss, var_list=generator_params)
generator_apply_grads_op = generator_optimizer.apply_gradients(generator_grads)

for v in encoder_params:
    tf.summary.histogram(v.name, v)
for v in generator_params:
    tf.summary.histogram(v.name, v)
summary_op = tf.summary.merge_all()

#
# Main loop
#

print('Start session')
global_iters = 0
start_epoch = 0

with tf.Session() as session:
    init = tf.global_variables_initializer()
    session.run([init, train_iterator_init_op, test_iterator_init_op, fixed_iterator_init_op])

    summary_writer = tf.summary.FileWriter(args.prefix+"/", graph=tf.get_default_graph())
    saver = tf.train.Saver(max_to_keep=None)
    if args.model_path is not None and tf.train.checkpoint_exists(args.model_path):
        saver.restore(session, tf.train.latest_checkpoint(args.model_path))
        print('Model restored from ' + args.model_path)
        ckpt = tf.train.get_checkpoint_state(args.model_path)
        global_iters = int(os.path.basename(ckpt.model_checkpoint_path).split('-')[1])
        start_epoch = (global_iters * args.batch_size) // args.train_size
    print('Global iters: ', global_iters)

    for iteration in range(iterations):
        epoch = global_iters * args.batch_size // args.train_size
        global_iters += 1

        x = session.run(train_next)
        z_p = np.random.normal(loc=0.0, scale=1.0, size=(args.batch_size, args.latent_dim))
        z_x, x_r, x_p = session.run([z, xr, generator_output], feed_dict={encoder_input: x, generator_input: z_p})

        _ = session.run([encoder_apply_grads_op], feed_dict={encoder_input: x, reconst_latent_input: z_x, sampled_latent_input: z_p})
        _ = session.run([generator_apply_grads_op], feed_dict={encoder_input: x, reconst_latent_input: z_x, sampled_latent_input: z_p})

        if global_iters % 10 == 0:
            summary, = session.run([summary_op], feed_dict={encoder_input: x})
            summary_writer.add_summary(summary, global_iters)

        if (global_iters % args.frequency) == 0:
            enc_loss_np, enc_l_ae_np, l_reg_z_np, l_reg_zr_ng_np, l_reg_zpp_ng_np, generator_loss_np, dec_l_ae_np, l_reg_zr_np, l_reg_zpp_np = \
             session.run([encoder_loss, l_ae, l_reg_z, l_reg_zr_ng, l_reg_zpp_ng, generator_loss, l_ae2, l_reg_zr, l_reg_zpp],
                         feed_dict={encoder_input: x, reconst_latent_input: z_x, sampled_latent_input: z_p})
            print('Epoch: {}/{}, iteration: {}/{}'.format(epoch+1, args.nb_epoch, iteration+1, iterations))
            print(' Enc_loss: {}, l_ae:{},  l_reg_z: {}, l_reg_zr_ng: {}, l_reg_zpp_ng: {}'.format(enc_loss_np, enc_l_ae_np, l_reg_z_np, l_reg_zr_ng_np, l_reg_zpp_ng_np))
            print(' Dec_loss: {}, l_ae:{}, l_reg_zr: {}, l_reg_zpp: {}'.format(generator_loss_np, dec_l_ae_np, l_reg_zr_np, l_reg_zpp_np))

        if ((global_iters % iterations_per_epoch == 0) and args.save_latent):
            utils.save_output(session, args.prefix, epoch, global_iters, args.batch_size, OrderedDict({encoder_input: test_next}), OrderedDict({"test_mean": z_mean, "test_log_var": z_log_var}), args.test_size)
            utils.save_output(session, args.prefix, epoch, global_iters, args.batch_size, OrderedDict({encoder_input: fixed_next}), OrderedDict({"train_mean": z_mean, "train_log_var": z_log_var}), args.latent_cloud_size)

            n_x = 5
            n_y = args.batch_size // n_x
            print('Save original images.')
            utils.plot_images(np.transpose(x, (0, 2, 3, 1)), n_x, n_y, "{}_original_epoch{}_iter{}".format(args.prefix, epoch + 1, global_iters), text=None)
            print('Save generated images.')
            utils.plot_images(np.transpose(x_p, (0, 2, 3, 1)), n_x, n_y, "{}_sampled_epoch{}_iter{}".format(args.prefix, epoch + 1, global_iters), text=None)
            print('Save reconstructed images.')
            utils.plot_images(np.transpose(x_r, (0, 2, 3, 1)), n_x, n_y, "{}_reconstructed_epoch{}_iter{}".format(args.prefix, epoch + 1, global_iters), text=None)

        if ((global_iters % iterations_per_epoch == 0) and ((epoch + 1) % 10 == 0)):
            if args.model_path is not None:
                saved = saver.save(session, args.model_path + "/model", global_step=global_iters)
                print('Saved model to ' + saved)
