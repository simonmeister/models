# --------------------------------------------------------
# Motion R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Simon Meister
# --------------------------------------------------------
import tensorflow as tf


def euler_to_rot(x, y, z, sine_inputs=False):
    """Compose 3d rotations (in batches) from angles.
    Args:
      x, y, z: tensor of shape (N, 1)
      sine_inputs: if true, inputs are given as angle sines with
        values in [-1, 1], if false, as raw angles in radians.
    Returns:
      rotations: tensor of shape (N, 3, 3)
    """
    x = tf.expand_dims(x, 1)
    y = tf.expand_dims(y, 1)
    z = tf.expand_dims(z, 1)

    if sine_inputs:
      sin_x = x
      sin_y = y
      sin_z = z
      cos_x = tf.sqrt(1 - tf.square(sin_x))
      cos_y = tf.sqrt(1 - tf.square(sin_y))
      cos_z = tf.sqrt(1 - tf.square(sin_z))
      cos_x = tf.check_numerics(cos_x, message='cos_x_', name='cos_x_')
      cos_y = tf.check_numerics(cos_y, message='cos_y_', name='cos_y_')
      cos_z = tf.check_numerics(cos_z, message='cos_z_', name='cos_z_')
    else:
      sin_x = tf.sin(x)
      sin_y = tf.sin(y)
      sin_z = tf.sin(z)
      cos_x = tf.cos(x)
      cos_y = tf.cos(y)
      cos_z = tf.cos(z)

    zero = tf.zeros_like(sin_x)
    one = tf.ones_like(sin_x)

    rot_x_1 = tf.stack([one, zero, zero], axis=2)
    rot_x_2 = tf.stack([zero, cos_x, -sin_x], axis=2)
    rot_x_3 = tf.stack([zero, sin_x, cos_x], axis=2)
    rot_x = tf.concat([rot_x_1, rot_x_2, rot_x_3], axis=1)

    rot_y_1 = tf.stack([cos_y, zero, sin_y], axis=2)
    rot_y_2 = tf.stack([zero, one, zero], axis=2)
    rot_y_3 = tf.stack([-sin_y, zero, cos_y], axis=2)
    rot_y = tf.concat([rot_y_1, rot_y_2, rot_y_3], axis=1)

    rot_z_1 = tf.stack([cos_z, -sin_z, zero], axis=2)
    rot_z_2 = tf.stack([sin_z, cos_z, zero], axis=2)
    rot_z_3 = tf.stack([zero, zero, one], axis=2)
    rot_z = tf.concat([rot_z_1, rot_z_2, rot_z_3], axis=1)

    return rot_z @ rot_x @ rot_y


def motion_loss(pred, target, weights):
  """
  Args:
    pred: tensor of shape [batch_size, num_anchors, 9]
    target: tensor of shape [batch_size, num_anchors, 15]
    weights: tensor of shape [batch_size, num_anchors]
  Returns:
    loss: a tensor of shape [batch_size, num_anchors]
  """
  batch_size, num_anchors = tf.unstack(tf.shape(pred)[:2])

  err_angle, err_trans, err_pivot = _motion_losses(
      tf.reshape(pred, [-1, 9]),
      tf.reshape(target, [-1, 15]))

  total_err = err_angle + err_trans + err_pivot
  return tf.reshape(total_err, [batch_size, num_anchors]) * weights


def _motion_losses(pred, target):
  """
  Args:
    pred: tensor of shape [num_predictions, 9] containing predicted
      angle sines, translation and pivot
    target: tensor of shape [num_predictions, 15] containing
      target rotation matrix (flat), translation and pivot.
  Returns:
    losses: three-tuple of tensors of shape [num_predictions] representing the
      rotation, translation and pivot loss for each instance
  """
  pred = postprocess_detection_motions(pred)
  rot = tf.reshape(pred[:, 0:9], [-1, 3, 3])
  trans = pred[:, 9:12]
  pivot = pred[:, 12:15]

  gt_rot = tf.reshape(target[:, 0:9], [-1, 3, 3])
  gt_trans = target[:, 9:12]
  gt_pivot = target[:, 12:15]

  rot_T = tf.transpose(rot, [0, 2, 1])
  #d_rot = rot_T @ gt_rot
  d_rot = rot_T @ gt_rot
  d_trans = gt_trans - trans
  #d_trans = tf.squeeze(rot_T @ tf.reshape(gt_trans - trans, [-1, 3, 1]),
  #                     axis=2)
  d_pivot = gt_pivot - pivot

  err_angle = tf.acos(tf.clip_by_value((tf.trace(d_rot) - 1) / 2, -1, 1))
  err_trans = tf.norm(d_trans, axis=1)
  err_pivot = tf.norm(d_pivot, axis=1)

  return err_angle, err_trans, err_pivot


def postprocess_detection_motions(pred):
  """Convert predicted motions to use matrix representation for rotations.
  Restrict range of angle sines to [-1, 1]"""
  angle_sines = pred
  #angle_sines = tf.clip_by_value(pred[:, 0:3], -1, 1)
  rot = euler_to_rot(angle_sines[:, 0], angle_sines[:, 1], angle_sines[:, 2])
  rot_flat = tf.reshape(rot, [-1, 9])
  return tf.concat([rot_flat, pred[:, 3:]], axis=1)


def postprocess_camera_motion(pred):
  return postprocess_detection_motions(tf.expand_dims(pred, 0))[0, :]


def camera_motion_loss(pred, target):
  """Compute loss between predicted and ground truth camera motion.
  Args:
    pred: tensor of shape [batch_size, 6] containing predicted
      angle sines and translation.
    target: tensor of shape [batch_size, 12] containing
      target rotation matrix and translation.
  Returns:
    losses: a scalar
  """
  batch_size = tf.unstack(tf.shape(pred))[0]
  mock_pivot = tf.zeros([batch_size, 3])
  err_angle, err_trans, _ = _motion_losses(
    tf.concat([pred, mock_pivot], axis=1),
    tf.concat([target, mock_pivot], axis=1))

  return err_angle + err_trans


def get_3D_coords(depth, camera_intrinsics):
  def _pixels_to_3d(x, y, d):
      x = tf.expand_dims(tf.expand_dims(x, 0), 3)
      y = tf.expand_dims(tf.expand_dims(y, 0), 3)
      f, x0, y0 = tf.unstack(camera_intrinsics)
      factor = d / f
      X = (x - x0) * factor
      Y = (y - y0) * factor
      Z = d
      return X, Y, Z

  num, height, width = tf.unstack(tf.shape(depth))[:3]
  ys = tf.cast(tf.range(height), tf.float32)
  xs = tf.cast(tf.range(width), tf.float32)
  x, y = tf.meshgrid(xs, ys)
  X, Y, Z = _pixels_to_3d(x, y, depth)
  XYZ = tf.concat([X, Y, Z], axis=3)
  return XYZ
