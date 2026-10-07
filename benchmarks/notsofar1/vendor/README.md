Unmodified NOTSOFAR/CHiME-8 text normalization implementation, MIT license.

Source: https://github.com/microsoft/NOTSOFAR1-Challenge/tree/6f58e08b008f7530ba4141f0aeb02447c70b6fd7/utils/text_norm_whisper_like

Original filenames and LICENSE retained. `chime8/hashes.json` records SHA256 checksums.
This is normalization code and spelling dictionaries, not benchmark recordings or GT.
No Whisper model or model inference is used. Vendoring the small official normalizer
makes offline unit tests and repeat scoring independent of remote source changes.
