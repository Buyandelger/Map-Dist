import unittest
from concurrent.futures import Future
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import ElementMap
from distribution import calculate_surface
from record_panel import RecordPanel
from records import Element, Measurement, Point


class SelectionTests(unittest.TestCase):
    def make_app(self):
        app = ElementMap.__new__(ElementMap)
        app.root = Mock()
        app.canvas = Mock()
        app.legend = Mock()
        app.clear_button = Mock()
        app.refresh_calculate = Mock()
        app.draw_points = Mock()
        app.records = SimpleNamespace(point_id=1, element_id=10, active_date=date(2026, 1, 1))
        app.playback_selecting = False
        app.stop_playback = Mock()
        app.map_selection = 1
        app.distribution_selection = (1, 9, 0)
        app.distribution_request = 1
        app.distribution_job = None
        app.measurement_revision = 0
        app.pending = True
        app.surface = object()
        return app

    def test_selection_clears_old_overlay_and_rejects_old_result(self):
        app = self.make_app()
        app.selection_changed()
        self.assertIsNone(app.surface)
        self.assertFalse(app.pending)
        app.root.after.assert_called_once_with(120, app.calculate_distribution)
        future = Future()
        future.set_result(object())
        app.show_distribution(future, 'Point', 'As', 0, 1)
        self.assertIsNone(app.surface)
        app.selection_changed()
        self.assertEqual(app.root.after.call_count, 1)

    def test_clear_invalidates_pending_result(self):
        app = self.make_app()
        app.distribution_job = 'scheduled'
        app.clear_surface()
        app.root.after_cancel.assert_called_once_with('scheduled')
        app.show_distribution(Future(), 'Point', 'As', 0, 1)
        app.root.after.assert_not_called()

    def test_point_change_preserves_element_name(self):
        panel = RecordPanel.__new__(RecordPanel)
        panel.point_id = 1
        panel.element_id = 11
        panel.elements = {11: Element(1, 'Cr'), 20: Element(2, 'As'), 21: Element(2, 'cr')}
        panel.point_list = Mock()
        panel.point_list.selected_id.return_value = 2
        panel.status = Mock()
        panel.show_elements = Mock()
        panel.point_selected()
        panel.show_elements.assert_called_once_with(21)

    def test_marker_click_uses_position_not_current_tag(self):
        app = self.make_app()
        app.records = Mock(coordinates=None)
        app.canvas.find_overlapping.return_value = (4, 5)
        app.canvas.gettags.return_value = ('point-2', 'points')
        app.pick_location(SimpleNamespace(x=100, y=200))
        app.records.select_point.assert_called_once_with(2)
        app.canvas.gettags.assert_called_once_with(5)

    @patch('distribution.arcgis_idw', return_value=None)
    def test_elements_produce_separate_surfaces(self, _):
        points = {1: Point('A', 45, 99), 2: Point('B', 46, 100)}
        measurements = [Measurement(i, '2026-01-01', name, str(value))
                        for name, values in [('As', [1, 2]), ('Cr', [100, 200])]
                        for i, value in enumerate(values, 1)]
        arsenic = calculate_surface(points, measurements, 1, 'As')
        chromium = calculate_surface(points, measurements, 1, 'Cr')
        self.assertEqual((arsenic.minimum, arsenic.maximum), (1, 2))
        self.assertEqual((chromium.minimum, chromium.maximum), (100, 200))
        self.assertNotEqual(arsenic.rows, chromium.rows)


if __name__ == '__main__':
    unittest.main()
