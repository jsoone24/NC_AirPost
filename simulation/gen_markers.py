#!/usr/bin/env python3
"""Generate per-station AprilTag/ArUco marker models with UNIQUE ids (DICT_4X4_50, id = station
index). Each station gets its own tag so the drone only precision-lands on the *intended* pad.
Creates gz/models/airpost_tag_<id>/ {tag.png, model.sdf, model.config}. Run with the detector
venv (has cv2). Usage: gen_markers.py [N]
"""
import os, sys
import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "gz/models")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)

SDF = '''<?xml version="1.0" ?>
<!-- AirPost landing marker, ArUco DICT_4X4_50 id {i}, with white quiet-zone border.
     Detectable marker square = 0.5 m; white border to 0.8 m. -->
<sdf version="1.9">
  <model name="airpost_tag_{i}">
    <static>true</static>
    <link name="base">
      <visual name="quiet_zone">
        <geometry><plane><normal>0 0 1</normal><size>0.8 0.8</size></plane></geometry>
        <material><ambient>1 1 1 1</ambient><diffuse>1 1 1 1</diffuse><specular>0.3 0.3 0.3 1</specular></material>
      </visual>
      <visual name="marker">
        <pose>0 0 0.002 0 0 0</pose>
        <geometry><plane><normal>0 0 1</normal><size>0.5 0.5</size></plane></geometry>
        <material>
          <ambient>1 1 1 1</ambient><diffuse>1 1 1 1</diffuse>
          <pbr><metal><albedo_map>model://airpost_tag_{i}/tag_{i}.png</albedo_map></metal></pbr>
        </material>
      </visual>
    </link>
  </model>
</sdf>
'''
CFG = '''<?xml version="1.0"?>
<model><name>airpost_tag_{i}</name><version>1.0</version><sdf version="1.9">model.sdf</sdf>
<description>AirPost landing marker, unique ArUco id {i}.</description></model>
'''

for i in range(N):
    d = os.path.join(MODELS, f"airpost_tag_{i}")
    os.makedirs(d, exist_ok=True)
    img = cv2.aruco.generateImageMarker(DICT, i, 400)
    cv2.imwrite(os.path.join(d, f"tag_{i}.png"), img)
    open(os.path.join(d, "model.sdf"), "w").write(SDF.format(i=i))
    open(os.path.join(d, "model.config"), "w").write(CFG.format(i=i))
print(f"generated {N} unique-id tag models in {MODELS}/airpost_tag_*")
