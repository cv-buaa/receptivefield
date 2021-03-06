from typing import Tuple, Callable, List

import numpy as np
import tensorflow as tf

from receptivefield.base import ReceptiveField
from receptivefield.logging import get_logger
from receptivefield.types import ImageShape, GridShape, GridPoint, \
    FeatureMapDescription

_logger = get_logger()


def _get_tensor_shape(tensor: tf.Tensor) -> List[int]:
    """
    Parse TensorShape to python tuple:
    Example: TensorShape([
        Dimension(1), Dimension(8), Dimension(8), Dimension(512)
    ]) will return [1, 8, 8, 32]
    :param tensor: tensorflow Tensor
    :return: integer type tuple
    """
    return list(map(int, tensor.get_shape()))


def _define_fm_gradient(
        input_tensor: tf.Tensor,
        output_tensor: tf.Tensor,
        receptive_field_mask: tf.Tensor
) -> tf.Tensor:
    """
    Define gradient of feature map w.r.t. the input image.

    :param input_tensor: an input image tensor [h, w, 3]
    :param output_tensor: an feature map tensor [fm_h, fm_w, num_channels]
    :param receptive_field_mask: a backpropagation mask [fm_h, fm_w, 1]

    :return: a gradient tensor [h, w, 3]
    """
    x = tf.reduce_mean(output_tensor, -1, keep_dims=True)
    fake_loss = x * receptive_field_mask
    fake_loss = tf.reduce_mean(fake_loss)
    # define gradient w.r.t. image
    return tf.gradients(fake_loss, input_tensor)[0]


def _get_gradient_from_grid_points(
        rf_extractor: ReceptiveField,
        points: List[GridPoint],
        intensity: float = 1.0
) -> List[np.ndarray]:
    """
    Computes gradient at input image tenor generated by
    point-like perturbation at output grid location given by
    @point coordinates.

    :param rf_extractor: and instance of TFReceptiveField or
        TFFeatureMapsReceptiveField.
    :param points: source coordinate of the backpropagated gradient for each
        feature map.
    :param intensity: scale of the gradient, default = 1
    :return gradient maps for each feature map
    """

    input_shape = rf_extractor._input_shape.replace(n=1)
    output_feature_maps = []
    for fm in range(rf_extractor.num_feature_maps):
        output_shape = rf_extractor._output_shapes[fm].replace(n=1)
        output_feature_map = np.zeros(shape=output_shape)
        output_feature_map[:, points[fm].y, points[fm].x, 0] = intensity
        output_feature_maps.append(output_feature_map)

    return rf_extractor._gradient_function(
        output_feature_maps, np.zeros(shape=input_shape)
    )


class TFReceptiveField(ReceptiveField):
    def __init__(self, model_func: Callable[[ImageShape], tf.Tensor]):
        """
        :param model_func: model creation function. Function which accepts image
            shape [H, W, C] and returns tensorflow graph.
        """
        self._session: tf.Session = None
        super().__init__(model_func)

    def _prepare_gradient_func(
        self, input_shape: ImageShape, input_tensor: str, output_tensors: List[str]
    ) -> Tuple[Callable, GridShape, List[GridShape]]:
        """
        Computes gradient function and additional parameters. Note
        that the receptive field parameters like stride or size, do not
        depend on input image shape. However, if the RF of original network
        is bigger than input_shape this method will fail. Hence it is
        recommended to increase the input shape.

        :param input_shape: shape of the input image. Used in @model_func.
        :param input_tensor: name of the input image tensor.
        :param output_tensors: a list of names of the target
            feature map tensors.

        :returns
            gradient_function: a function which returns gradient w.r.t. to
                the input image.
            input_shape: a shape of the input image tensor.
            output_shape: a list shapes of the output feature map tensors.
        """

        if self._session is not None:
            tf.reset_default_graph()
            self._session.close()

        with tf.Graph().as_default() as graph:
            with tf.variable_scope("", reuse=tf.AUTO_REUSE):

                # this function will create default graph
                _ = self._model_func(ImageShape(*input_shape))

                # default_graph = tf.get_default_graph()
                # get graph tensors by names
                input_tensor = graph.get_operation_by_name(input_tensor).outputs[0]
                input_shape = _get_tensor_shape(input_tensor)

                grads = []
                receptive_field_masks = []
                output_shapes = []

                for output_tensor in output_tensors:
                    output_tensor = graph.get_operation_by_name(output_tensor).outputs[0]

                    # shapes
                    output_shape = _get_tensor_shape(output_tensor)
                    output_shape = (1, output_shape[1], output_shape[2], 1)
                    output_shapes.append(output_shape)

                    # define loss function
                    receptive_field_mask = tf.placeholder(
                        tf.float32, shape=output_shape, name="grid"
                    )
                    grad = _define_fm_gradient(
                        input_tensor, output_tensor, receptive_field_mask
                    )
                    grads.append(grad)
                    receptive_field_masks.append(receptive_field_mask)

                _logger.info(f"Feature maps shape: {output_shapes}")
                _logger.info(f"Input shape       : {input_shape}")

            self._session = tf.Session(graph=graph)
            self._session.run(tf.global_variables_initializer())

            def gradient_fn(fm_masks, input_image):
                fetch_dict = {
                    mask_t: mask_np
                    for mask_t, mask_np in zip(receptive_field_masks, fm_masks)
                }
                fetch_dict[input_tensor] = input_image
                return self._session.run(grads, feed_dict=fetch_dict)

        return (
            gradient_fn,
            GridShape(*input_shape),
            [GridShape(*output_shape) for output_shape in output_shapes],
        )

    def _get_gradient_from_grid_points(
        self, points: List[GridPoint], intensity: float = 1.0
    ) -> List[np.ndarray]:
        """
        Computes gradient at image tensor generated by
        point-like perturbation at output grid location given by
        @point coordinates.

        :param points: source coordinate of the backpropagated gradient for each
            feature map.
        :param intensity: scale of the gradient, default = 1
        :return gradient maps for each feature map
        """

        return _get_gradient_from_grid_points(
            self,
            points=points,
            intensity=intensity
        )

    def compute(
            self,
            input_shape: ImageShape,
            input_tensor: str,
            output_tensors: List[str]
    ) -> List[FeatureMapDescription]:

        """
        Compute ReceptiveFieldDescription of given model for image of
        shape input_shape [H, W, C]. If receptive field of the network
        is bigger thant input_shape this method will raise exception.
        In order to solve with problem try to increase input_shape.

        :param input_shape: shape of the input image e.g. (224, 224, 3)
        :param input_tensor: name of the input tensor
        :param output_tensors: a list of names of the target feature map tensors.

        :return a list of estimated FeatureMapDescription for each feature
            map.
        """

        return super().compute(
            input_shape=input_shape,
            input_tensor=input_tensor,
            output_tensors=output_tensors
        )


class TFFeatureMapsReceptiveField(ReceptiveField):
    def __init__(self, model_func: Callable[[tf.Tensor], List[tf.Tensor]]):
        """
        :param model_func: model creation function. Function which
            accepts image_tensor of shape [1, H, W, C] as an input and
            returns list of tensors which correspond to selected feature maps.
        """
        self._session = None
        super().__init__(model_func)

    def _prepare_gradient_func(
        self, input_shape: ImageShape
    ) -> Tuple[Callable, GridShape, List[GridShape]]:

        """
        Computes gradient function and additional parameters. Note
        that the receptive field parameters like stride or size, do not
        depend on input image shape. However, if the RF of original network
        is bigger than input_shape this method will fail. Hence it is
        recommended to increase the input shape.

        :param input_shape: shape of the input image. Used in @model_func.

        :returns
            gradient_function: a function which returns gradient w.r.t. to
                the input image
            input_shape: a shape of the input image tensor
            output_shapes: a list shapes of the output feature map tensors
        """

        if self._session is not None:
            tf.reset_default_graph()
            self._session.close()

        with tf.Graph().as_default() as graph:
            with tf.variable_scope("", reuse=tf.AUTO_REUSE):

                input_tensor = tf.placeholder(
                    tf.float32, shape=[1, *input_shape], name="input_image"
                )
                input_shape = _get_tensor_shape(input_tensor)
                feature_maps = self._model_func(input_tensor)

                grads = []
                receptive_field_masks = []
                output_shapes = []

                for output_tensor in feature_maps:
                    # shapes
                    output_shape = _get_tensor_shape(output_tensor)
                    output_shape = (1, output_shape[1], output_shape[2], 1)
                    output_shapes.append(output_shape)

                    # define loss function
                    receptive_field_mask = tf.placeholder(
                        tf.float32, shape=output_shape, name="grid"
                    )
                    grad = _define_fm_gradient(
                        input_tensor, output_tensor, receptive_field_mask
                    )
                    grads.append(grad)
                    receptive_field_masks.append(receptive_field_mask)

            _logger.info(f"Feature maps shape: {output_shapes}")
            _logger.info(f"Input shape       : {input_shape}")

            self._session = tf.Session(graph=graph)
            self._session.run(tf.global_variables_initializer())

            def gradient_fn(
                fm_masks: List[np.ndarray], input_image: np.ndarray
            ) -> List[np.ndarray]:
                fetch_dict = {
                    mask_t: mask_np
                    for mask_t, mask_np in zip(receptive_field_masks, fm_masks)
                }
                fetch_dict[input_tensor] = input_image
                return self._session.run(grads, feed_dict=fetch_dict)

        return (
            gradient_fn,
            GridShape(*input_shape),
            [GridShape(*output_shape) for output_shape in output_shapes],
        )

    def _get_gradient_from_grid_points(
            self, points: List[GridPoint], intensity: float = 1.0
    ) -> List[np.ndarray]:
        """
        Computes gradient at input image tensor generated by
        point-like perturbation at output grid location given by
        @point coordinates.

        :param points: source coordinate of the backpropagated gradient for each
            feature map.
        :param intensity: scale of the gradient, default = 1
        :return gradient maps for each feature map
        """
        return _get_gradient_from_grid_points(
            self,
            points=points,
            intensity=intensity
        )

    def compute(self, input_shape: ImageShape) -> List[FeatureMapDescription]:
        """
        Compute ReceptiveFieldDescription of given model for image of
        shape input_shape [H, W, C]. If receptive field of the network
        is bigger thant input_shape this method will raise exception.
        In order to solve with problem try to increase input_shape.

        :param input_shape: shape of the input image e.g. (224, 224, 3)

        :return a list of estimated FeatureMapDescription for each feature
            map.
        """
        return super().compute(input_shape=input_shape)
