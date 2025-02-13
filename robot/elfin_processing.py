#--------------------------------------------------------------------------
# Software:     InVesalius - Software de Reconstrucao 3D de Imagens Medicas
# Copyright:    (C) 2001  Centro de Pesquisas Renato Archer
# Homepage:     http://www.softwarepublico.gov.br
# Contact:      invesalius@cti.gov.br
# License:      GNU - GPL 2 (LICENSE.txt/LICENCA.txt)
#--------------------------------------------------------------------------
#    Este programa e software livre; voce pode redistribui-lo e/ou
#    modifica-lo sob os termos da Licenca Publica Geral GNU, conforme
#    publicada pela Free Software Foundation; de acordo com a versao 2
#    da Licenca.
#
#    Este programa eh distribuido na expectativa de ser util, mas SEM
#    QUALQUER GARANTIA; sem mesmo a garantia implicita de
#    COMERCIALIZACAO ou de ADEQUACAO A QUALQUER PROPOSITO EM
#    PARTICULAR. Consulte a Licenca Publica Geral GNU para obter mais
#    detalhes.
#--------------------------------------------------------------------------
import numpy as np
import cv2
from time import time

import transformations as tr
import constants as const

def coordinates_to_transformation_matrix(position, orientation, axes='sxyz'):
    """
    Transform vectors consisting of position and orientation (in Euler angles) in 3d-space into a 4x4
    transformation matrix that combines the rotation and translation.
    :param position: A vector of three coordinates.
    :param orientation: A vector of three Euler angles in degrees.
    :param axes: The order in which the rotations are done for the axes. See transformations.py for details. Defaults to 'sxyz'.
    :return: The transformation matrix (4x4).
    """
    a, b, g = np.radians(orientation)

    r_ref = tr.euler_matrix(a, b, g, axes=axes)
    t_ref = tr.translation_matrix(position)

    m_img = tr.concatenate_matrices(t_ref, r_ref)

    return m_img

def transformation_matrix_to_coordinates(matrix, axes='sxyz'):
    """
    Given a matrix that combines the rotation and translation, return the position and the orientation
    determined by the matrix. The orientation is given as three Euler angles.
    The inverse of coordinates_of_transformation_matrix when the parameter 'axes' matches.
    :param matrix: A 4x4 transformation matrix.
    :param axes: The order in which the rotations are done for the axes. See transformations.py for details. Defaults to 'sxyz'.
    :return: The position (a vector of length 3) and Euler angles for the orientation in degrees (a vector of length 3).
    """
    angles = tr.euler_from_matrix(matrix, axes=axes)
    angles_as_deg = np.degrees(angles)

    translation = tr.translation_from_matrix(matrix)

    return translation, angles_as_deg

def compute_marker_transformation(coord_raw, obj_ref_mode):
    m_probe = coordinates_to_transformation_matrix(
        position=coord_raw[obj_ref_mode, :3],
        orientation=coord_raw[obj_ref_mode, 3:],
        axes='rzyx',
    )
    return m_probe

def transformation_tracker_to_robot(m_tracker_to_robot, tracker_coord):
    M_tracker = coordinates_to_transformation_matrix(
        position=tracker_coord[:3],
        orientation=tracker_coord[3:6],
        axes='rzyx',
    )
    M_tracker_in_robot = m_tracker_to_robot @ M_tracker

    translation, angles_as_deg = transformation_matrix_to_coordinates(M_tracker_in_robot, axes='rzyx')
    tracker_in_robot = list(translation) + list(angles_as_deg)

    return tracker_in_robot

def transform_tracker_to_robot(m_tracker_to_robot, coord_tracker):
    probe_tracker_in_robot = transformation_tracker_to_robot(m_tracker_to_robot, coord_tracker[0])
    ref_tracker_in_robot = transformation_tracker_to_robot(m_tracker_to_robot, coord_tracker[1])
    obj_tracker_in_robot = transformation_tracker_to_robot(m_tracker_to_robot, coord_tracker[2])

    if probe_tracker_in_robot is None:
        probe_tracker_in_robot = coord_tracker[0]
        ref_tracker_in_robot = coord_tracker[1]
        obj_tracker_in_robot = coord_tracker[2]

    return np.vstack([probe_tracker_in_robot, ref_tracker_in_robot, obj_tracker_in_robot])


class KalmanTracker:
    """
    Kalman filter to avoid sudden fluctuation from tracker device.
    The filter strength can be set by the cov_process, and cov_measure parameter
    It is required to create one instance for each variable (x, y, z, a, b, g)
    """
    def __init__(self,
                 state_num=2,
                 covariance_process=0.001,
                 covariance_measure=0.1):

        self.state_num = state_num
        measure_num = 1

        # The filter itself.
        self.filter = cv2.KalmanFilter(state_num, measure_num, 0)

        self.state = np.zeros((state_num, 1), dtype=np.float32)
        self.measurement = np.array((measure_num, 1), np.float32)
        self.prediction = np.zeros((state_num, 1), np.float32)


        self.filter.transitionMatrix = np.array([[1, 1],
                                                 [0, 1]], np.float32)
        self.filter.measurementMatrix = np.array([[1, 1]], np.float32)
        self.filter.processNoiseCov = np.array([[1, 0],
                                                [0, 1]], np.float32) * covariance_process
        self.filter.measurementNoiseCov = np.array([[1]], np.float32) * covariance_measure

    def update_kalman(self, measurement):
        self.prediction = self.filter.predict()
        self.measurement = np.array([[np.float32(measurement[0])]])

        self.filter.correct(self.measurement)
        self.state = self.filter.statePost


class TrackerProcessing:
    def __init__(self):
        self.coord_vel = []
        self.timestamp = []
        self.velocity_vector = []
        self.kalman_coord_vector = []
        self.velocity_std = 0
        self.m_tracker_to_robot = None
        self.matrix_tracker_fiducials = 3*[None]

        self.tracker_stabilizers = [KalmanTracker(
            state_num=2,
            covariance_process=0.001,
            covariance_measure=0.1) for _ in range(6)]

    def SetMatrixTrackerFiducials(self, matrix_tracker_fiducials):
        self.matrix_tracker_fiducials = matrix_tracker_fiducials

    def kalman_filter(self, coord_tracker):
        kalman_array = []
        pose_np = np.array((coord_tracker[:3], coord_tracker[3:])).flatten()
        for value, ps_stb in zip(pose_np, self.tracker_stabilizers):
            ps_stb.update_kalman([value])
            kalman_array.append(ps_stb.state[0])
        coord_kalman = np.hstack(kalman_array)

        self.kalman_coord_vector.append(coord_kalman[:3])
        if len(self.kalman_coord_vector) < 20: #avoid initial fluctuations
            coord_kalman = coord_tracker
            print('initializing filter')
        else:
            del self.kalman_coord_vector[0]

        return coord_kalman

    def estimate_head_velocity(self, coord_vel, timestamp):
        coord_vel = np.vstack(np.array(coord_vel))
        coord_init = coord_vel[:int(len(coord_vel) / 2)].mean(axis=0)
        coord_final = coord_vel[int(len(coord_vel) / 2):].mean(axis=0)
        velocity = (coord_final - coord_init)/(timestamp[-1] - timestamp[0])
        distance = (coord_final - coord_init)

        return velocity, distance

    def compute_versors(self, init_point, final_point):
        init_point = np.array(init_point)
        final_point = np.array(final_point)
        norm = (sum((final_point - init_point) ** 2)) ** 0.5
        versor_factor = (((final_point-init_point) / norm) * const.ROBOT_VERSOR_SCALE_FACTOR).tolist()

        return versor_factor

    def compute_arc_motion(self, current_robot_coordinates, head_center_coordinates, new_robot_coordinates):
        head_center = head_center_coordinates[0], head_center_coordinates[1], head_center_coordinates[2], \
                      new_robot_coordinates[3], new_robot_coordinates[4], new_robot_coordinates[5]

        versor_factor_move_out = self.compute_versors(head_center, current_robot_coordinates)
        init_move_out_point = current_robot_coordinates[0] + versor_factor_move_out[0], \
                              current_robot_coordinates[1] + versor_factor_move_out[1], \
                              current_robot_coordinates[2] + versor_factor_move_out[2], \
                              current_robot_coordinates[3], current_robot_coordinates[4], current_robot_coordinates[5]

        middle_point = ((new_robot_coordinates[0] + current_robot_coordinates[0]) / 2,
                        (new_robot_coordinates[1] + current_robot_coordinates[1]) / 2,
                        (new_robot_coordinates[2] + current_robot_coordinates[2]) / 2,
                        0, 0, 0)
        versor_factor_middle_arc = (np.array(self.compute_versors(head_center, middle_point))) * 2
        middle_arc_point = middle_point[0] + versor_factor_middle_arc[0], \
                           middle_point[1] + versor_factor_middle_arc[1], \
                           middle_point[2] + versor_factor_middle_arc[2]

        versor_factor_arc = self.compute_versors(head_center, new_robot_coordinates)
        final_ext_arc_point = new_robot_coordinates[0] + versor_factor_arc[0], \
                              new_robot_coordinates[1] + versor_factor_arc[1], \
                              new_robot_coordinates[2] + versor_factor_arc[2], \
                              new_robot_coordinates[3], new_robot_coordinates[4], new_robot_coordinates[5], 0

        target_arc = middle_arc_point + final_ext_arc_point

        return init_move_out_point, target_arc

    def compute_head_move_threshold(self, current_ref):
        """
        Checks if the head velocity is bellow the threshold
        """
        self.coord_vel.append(current_ref)
        self.timestamp.append(time())
        if len(self.coord_vel) >= 10:
            head_velocity, head_distance = self.estimate_head_velocity(self.coord_vel, self.timestamp)
            self.velocity_vector.append(head_velocity)

            del self.coord_vel[0]
            del self.timestamp[0]

            if len(self.velocity_vector) >= 30:
                self.velocity_std = np.std(self.velocity_vector)
                del self.velocity_vector[0]

            if self.velocity_std > const.ROBOT_HEAD_VELOCITY_THRESHOLD:
                print('Velocity threshold activated')
                return False
            else:
                return True

        return False

    def compute_head_move_compensation(self, current_head, m_change_robot_to_head):
        """
        Estimates the new robot position to reach the target
        """
        M_current_head = coordinates_to_transformation_matrix(
            position=current_head[:3],
            orientation=current_head[3:6],
            axes='rzyx',
        )
        m_robot_new = M_current_head @ m_change_robot_to_head

        translation, angles_as_deg = transformation_matrix_to_coordinates(m_robot_new, axes='sxyz')
        new_robot_position = list(translation) + list(angles_as_deg)

        return new_robot_position

    def estimate_head_center(self, current_head):
        """
        Estimates the actual head center position using fiducials
        """
        m_probe_head_left, m_probe_head_right, m_probe_head_nasion = self.matrix_tracker_fiducials
        m_current_head = compute_marker_transformation(np.array([current_head]), 0)

        m_ear_left_new = m_current_head @ m_probe_head_left
        m_ear_right_new = m_current_head @ m_probe_head_right

        return (m_ear_left_new[:3, -1] + m_ear_right_new[:3, -1])/2

    def correction_distance_calculation_target(self, coord_inv, actual_point):
        """
        Estimates the Euclidean distance between the actual position and the target
        """
        correction_distance_compensation = np.sqrt((coord_inv[0]-actual_point[0]) ** 2 +
                                                   (coord_inv[1]-actual_point[1]) ** 2 +
                                                   (coord_inv[2]-actual_point[2]) ** 2)

        return correction_distance_compensation

    def estimate_robot_target_length(self, robot_target):
        """
        Estimates the length of the 3D vector of the robot target
        """
        robot_target_length = np.sqrt(robot_target[0] ** 2 + robot_target[1] ** 2 + robot_target[2] ** 2)

        return robot_target_length
