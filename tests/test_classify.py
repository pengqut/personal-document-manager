import os
import sys
import unittest

import classify

class ClassifyTests(unittest.TestCase):

    # A pdf is a document
    def test_pdf_is_document(self):
        self.assertEqual(classify.classify('report.pdf'), 'document')

    # An mp3 is audio
    def test_mp3_is_audio(self):
        self.assertEqual(classify.classify('song.mp3'), 'audio')

    # A jpg is an image
    def test_jpg_is_image(self):
        self.assertEqual(classify.classify('photo.jpg'), 'image')

    # An mp4 is a video
    def test_mp4_is_video(self):
        self.assertEqual(classify.classify('movie.mp4'), 'video')

    # An unknown extension is other
    def test_unknown_extension_is_other(self):
        self.assertEqual(classify.classify('archive.xyz'), 'other')

    # No extension is other
    def test_no_extension_is_other(self):
        self.assertEqual(classify.classify('README'), 'other')


if __name__ == '__main__':
    unittest.main()
