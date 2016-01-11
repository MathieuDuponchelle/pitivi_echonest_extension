import os, threading
import pickle
import cairo

from gi.repository import Gtk, Gdk, GLib
from pyechonest import track as echotrack

from pitivi.extensions import BaseExtension
from pitivi.medialibrary import COL_URI
from pitivi.utils.misc import hash_file
from pitivi.settings import get_dir, xdg_cache_home

try:
    from pitivi.timeline import renderer
except ImportError:
    import renderer

here = os.path.dirname(__file__)

METADATA_BLACKLIST = ("pyechostring", "codestring", "synchstring",
        "analysis_url", "rhythmstring", "echoprintstring", "meta",
        "_object_type", "audio_md5", "cache", "code_version",
        "decoder_version", "echoprint_version", "id", "md5",
        "rhythm_version", "sample_md5", "status", "synch_version")

LIST_TYPED_METADATA = ("segments", "tatums", "beats", "bars", "sections")

class AudioPreviewer:
    def __init__(self, track, darea, clip_filename):
        filename = hash_file(clip_filename) + ".wave"
        cache_dir = get_dir(os.path.join(xdg_cache_home(), "waves"))
        filename = os.path.join(cache_dir, filename)

        self.darea = darea

        with open(filename, "rb") as samples:
            self.__peaks = pickle.load(samples)

        self.__max_peak = max(self.__peaks)
        self.__track = track
        self.__surface = None
        self.__markers = []

        darea.connect('draw', self.draw_cb)

    def draw_cb(self, darea, context):
        rect = Gdk.cairo_get_clip_rectangle(context)
        clipped_rect = rect[1]
        width = int(darea.get_allocation().width)
        height = int(darea.get_allocation().height)

        self.__surface = renderer.fill_surface(self.__peaks[:],
                                             width,
                                             height,
                                             self.__max_peak)

            

        context.set_operator(cairo.OPERATOR_OVER)
        context.set_source_surface(self.__surface, 0, 0)


        context.paint()

        context.set_source_rgb(1.0, 1.0, 1.0)
        context.set_line_width(0.5)

        for marker in self.__markers:
            x = marker * width
            context.move_to(x, 0)
            context.line_to(x, height)

        context.stroke()

    def set_markers(self, markers):
        self.__markers = markers

class EchonestExtension(BaseExtension):
    EXTENSION_NAME = 'echonest-extension'

    def __init__(self, app):
        BaseExtension.__init__(self, app)
        self.__asset_menu_item = None
        self.__analysis_handler_id = 0
        self.__audio_previewer = None
        self.__current_builder = None
        self.__current_track = None

    def setup(self):
        self.app.gui.medialibrary.connect('populating-asset-menu',
                self.__add_asset_menu_item_cb)
        self.app.gui.timeline_ui.timeline.connect('populating-clip-menu',
                self.__add_clip_menu_item_cb)

    def __load_from_cache(self, filename):
        filename = hash_file(filename) + '.analysis'
        cache_dir = get_dir(os.path.join(xdg_cache_home(), "echonest"))
        filename = os.path.join(cache_dir, filename)
        try:
            with open(filename, 'rb') as f:
                return pickle.load(f)
        except IOError:
            return None

    def __save_to_cache(self, filename, track):
        filename = hash_file(filename) + '.analysis'
        cache_dir = get_dir(os.path.join(xdg_cache_home(), "echonest"))
        filename = os.path.join(cache_dir, filename)
        with open(filename, 'wb') as f:
            pickle.dump(track, f)

    def analysis_worker(self, filename, callback, user_data):
        track = self.__load_from_cache(filename)

        if not track:
            track = echotrack.track_from_filename(filename)
            track.get_analysis()
            self.__save_to_cache(filename, track)

        if (callback):
            callback(track, *user_data)

    def __analyse_track(self, filename, callback, user_data):
        t = threading.Thread(target=self.analysis_worker, args=(filename,
            callback, user_data))
        t.daemon = True
        t.start()

    def __add_clip_menu_item_cb(self, timeline, clip, menu):
        menu_item = Gtk.MenuItem.new_with_label("Echonest dialog")
        menu_item.connect('activate',
                self.__clip_dialog_cb, clip)
        menu.append(menu_item)

    def __fill_metadata_list(self, track):
        listbox = self.__current_builder.get_object('metadata-list')
        for name, value in sorted(track.__dict__.items()):
            if name in METADATA_BLACKLIST:
                continue

            if name in LIST_TYPED_METADATA:
                text = "Number of %s : %d" % (name, len(value))
            else:
                text = "%s : %s" % (name, str(value))

            label = Gtk.Label.new(text)
            label.set_halign (Gtk.Align.START)

            if name in LIST_TYPED_METADATA:
                listbox.prepend(label)
            else:
                listbox.insert(label, -1)

        listbox.show_all()

    def __prepare_beat_matcher(self, track, filename):
        darea = self.__current_builder.get_object('waveform_area')
        self.__audio_previewer = AudioPreviewer(track, darea, filename)
        darea.get_style_context().add_class("AudioUriSource")
        markers = [beat['start'] / track.duration for beat in track.beats]
        self.__audio_previewer.set_markers(markers)

        for id_ in ('range-combo', 'select-type-combo', 'distribution-combo',
                'step-spinner'):
            self.__current_builder.get_object(id_).set_sensitive(True)

        self.__compute_markers()

    def __display_track_analysis(self, track, builder, filename):
        if builder != self.__current_builder:
            return

        self.__current_track = track
        self.__fill_metadata_list(track)
        self.__prepare_beat_matcher(track, filename)

    def __compute_markers(self):
        b = self.__current_builder
        t = self.__current_track

        range_ = b.get_object('range-combo').get_active_id()
        selection_type = b.get_object('select-type-combo').get_active_id()
        distribution = b.get_object('distribution-combo').get_active_id()
        step = int(b.get_object('step-spinner').get_value())

        if step == 1:
            b.get_object('beat_label').set_text("beat")
        else:
            b.get_object('beat_label').set_text("beats")

        if range_ == 'full':
            markers = [m['start'] / t.duration for m in
                    self.__current_track.beats[0::step]]
        else:
            markers = []

        self.__audio_previewer.set_markers(markers)
        self.__audio_previewer.darea.queue_draw()

    def _matching_changed_cb(self, unused_widget):
        self.__compute_markers()

    def __clip_dialog_cb(self, widget, clip):
        clip = clip.bClip
        filename = GLib.filename_from_uri(clip.props.uri)[0]

        self.__current_builder = Gtk.Builder()
        self.__current_builder.add_from_file(os.path.join(here, 'clip-dialog.ui'))
        self.__current_builder.connect_signals(self)
        self.__current_builder.get_object('step-spinner').set_range(1, 100)
        dialog = self.__current_builder.get_object('clip-dialog')
        dialog.set_transient_for(self.app.gui)

        self.__analyse_track(filename, self.__display_track_analysis,
                (self.__current_builder, filename,))

        res = dialog.run()

        self.__current_builder = None

        # We gud
        dialog.destroy()

    def __add_asset_menu_item_cb(self, medialibrary, model_row, menu):
        menu_item = Gtk.MenuItem.new_with_label("Run echonest analysis")
        menu_item.connect('activate',
                self.__run_analysis_clicked_cb, model_row[COL_URI])
        menu.append(menu_item)

    def __run_analysis_clicked_cb(self, widget, asset_uri):
        self.__analyse_track(GLib.filename_from_uri(asset_uri)[0], None, None)

def get_extension_classes():
    return [EchonestExtension]
