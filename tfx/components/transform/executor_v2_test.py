# Lint as: python2, python3
# Copyright 2020 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for tfx.components.transform.executor.

With the native TF2 code path being exercised.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import tensorflow_transform as tft

from tfx.components.transform import executor_test


class ExecutorV2Test(executor_test.ExecutorTest):

  def _use_force_tf_compat_v1(self):
    return False


if __name__ == '__main__':
  # TODO(b/): remove once TFT post-0.25.0 released and depended on.
  if tft.__version__ > '0.25.0' and tf.version.VERSION >= '2.4':
    tf.test.main()
