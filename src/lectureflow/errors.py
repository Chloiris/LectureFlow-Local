from __future__ import annotations


class LectureFlowError(Exception):
    """Base error that is safe to present without a traceback."""

    exit_code = 2


class ConfigurationError(LectureFlowError):
    pass


class SourceError(LectureFlowError):
    pass


class StateError(LectureFlowError):
    pass


class CommandError(LectureFlowError):
    pass


class PrepareError(LectureFlowError):
    exit_code = 3


class StageNotImplementedError(LectureFlowError):
    exit_code = 4


class JobNotFoundError(LectureFlowError):
    pass


class SubtitleError(LectureFlowError):
    exit_code = 5


class SubtitleParseError(SubtitleError):
    pass


class NetworkError(SubtitleError):
    pass


class ASRUnavailableError(SubtitleError):
    pass


class MediaError(LectureFlowError):
    exit_code = 6


class MediaSizeLimitError(MediaError):
    pass


class FrameError(LectureFlowError):
    exit_code = 7
