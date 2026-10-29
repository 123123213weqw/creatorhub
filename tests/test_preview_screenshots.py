"""Offline documentation/image contracts; screenshot capture is a separate CLI."""
import re
import struct
import xml.etree.ElementTree as ET

import pytest

from preview.capture_screenshots import ROOT, SCENES, VIEWPORT
from preview.build_preview import GUIDE_IMAGES


def test_guide_images_have_capture_scenes():
    guide = (ROOT / "guide" / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r"images/([\w-]+\.png)", guide))
    names = [scene.filename for scene in SCENES]
    assert len(names) == len(set(names))
    assert referenced == set(GUIDE_IMAGES)
    assert referenced <= set(names)
    assert {scene.theme for scene in SCENES} == {"light", "dark"}
    assert {scene.action for scene in SCENES} >= {"monitor", "publish", "dm"}


@pytest.mark.parametrize("scene", SCENES, ids=lambda scene: scene.filename)
def test_capture_image_dimensions(scene):
    header = (ROOT / "assets" / "screenshots" / scene.filename).read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", header[16:24])
    assert width == VIEWPORT["width"]
    assert height >= VIEWPORT["height"]
    if scene.action == "monitor":
        assert height == VIEWPORT["height"], "Transient drawers use the actual viewport"


def test_publish_cover_has_no_external_dependencies():
    cover = ET.parse(ROOT / "preview" / "fixtures" / "publish-cover.svg").getroot()
    assert cover.tag == "{http://www.w3.org/2000/svg}svg"
    assert not cover.findall(".//{http://www.w3.org/2000/svg}script")
    assert not any("href" in key for node in cover.iter() for key in node.attrib)
