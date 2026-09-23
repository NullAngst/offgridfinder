# Third-party material

## Ultimate Vocal Remover (UVR)

- Project: https://github.com/Anjok07/ultimatevocalremovergui
- License: MIT (see the LICENSE file in that repository for the copyright line)
- Used here: `splitscope/resources/uvr_mdx_model_data.json` is UVR's MDX-Net model parameter table
  (model hash to FFT size, frequency bins, segment size, compensation and primary stem). The MDX-Net
  demixing loop in `splitscope/dsp/mdx.py` is a NumPy reimplementation of UVR's approach, written
  for this project.
- Model weights (Kim Vocal 2, UVR-MDX-NET Voc FT, Inst HQ 3, Karaoke 2) are downloaded from UVR's
  public model repository (https://github.com/TRvlvr/model_repo). They are published for free use but
  carry no explicit license of their own.

## KUIELab MDX-Net

- Project: https://github.com/kuielab/mdx-net
- License: MIT, Copyright (c) 2020 KINoAI
- Used here: the kuielab_a drums, bass and other models (distributed through the UVR model repository).

## MIT license text (applies to the two projects above)

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES
OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Libraries bundled in release builds

| Library | License |
|---|---|
| Qt 6 / PySide6 | LGPL-3.0 |
| NumPy, SciPy | BSD-3-Clause |
| ONNX Runtime | MIT |
| libsndfile (via soundfile) | LGPL-2.1 |
| PortAudio (via sounddevice) | MIT-style |
| ffmpeg (via imageio-ffmpeg) | LGPL / GPL depending on build |
| Python | PSF License |
