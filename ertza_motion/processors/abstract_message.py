# -*- coding: utf-8 -*-


class AbstractMessage(object):
    SEP = None

    @property
    def command(self):
        raise NotImplementedError()

    @property
    def target(self):
        raise NotImplementedError()

    @property
    def args(self):
        raise NotImplementedError()
