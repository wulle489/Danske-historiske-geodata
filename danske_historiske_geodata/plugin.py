import json
import re
from pathlib import Path
from qgis.PyQt.QtCore import QUrl, Qt
from qgis.PyQt.QtGui import QIcon, QDesktopServices
try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem,
    QPushButton, QHBoxLayout, QProgressBar, QMessageBox)
from qgis.core import QgsApplication, QgsTask, QgsProject, QgsVectorLayer, QgsRasterLayer, QgsProviderRegistry, QgsMessageLog, Qgis
from .download import fetch, fetch_dropbox

ROOT = Path(__file__).parent
MENU = 'Danske historiske geodata'

class DownloadTask(QgsTask):
    def __init__(self, entry, folder, callback):
        super().__init__('DHG: ' + entry['title'], QgsTask.Flag.CanCancel)
        self.entry, self.folder, self.callback = entry, folder, callback
        self.paths, self.error = [], ''
    def run(self):
        try:
            self.paths = fetch_dropbox(self.entry, self.folder.parent, self.setProgress, self.isCanceled) if self.entry.get('provider') == 'dropbox' else fetch(self.entry['record_id'], self.folder, self.setProgress, self.isCanceled, self.entry.get('extensions'))
            return not self.isCanceled()
        except Exception as exc:
            self.error = str(exc)
            return False
    def finished(self, result):
        if self.callback:
            self.callback(self, result)

class HistoricalDataPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dialog = None
        self.task = None
        self.catalog = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
        self.storage = Path(QgsApplication.qgisSettingsDirPath()) / 'historical_geodata'
    def initGui(self):
        self.action = QAction(QIcon(str(ROOT / 'icon.svg')), MENU, self.iface.mainWindow())
        self.action.triggered.connect(self.show)
        self.iface.addPluginToMenu(MENU, self.action)
        self.iface.addToolBarIcon(self.action)
    def unload(self):
        if self.task:
            self.task.callback = None
            try:
                self.task.progressChanged.disconnect()
            except (TypeError, RuntimeError) as exc:
                QgsMessageLog.logMessage(f"Kunne ikke afbryde progressignal: {exc}", MENU, Qgis.MessageLevel.Warning)
            self.task.cancel()
        if self.dialog:
            self.dialog.close()
            self.dialog.deleteLater()
        self.iface.removePluginMenu(MENU, self.action)
        self.iface.removeToolBarIcon(self.action)
    def show(self):
        if self.dialog is None:
            d = self.dialog = QDialog(self.iface.mainWindow())
            d.setWindowTitle(MENU)
            d.resize(850, 480)
            layout = QVBoxLayout(d)
            layout.addWidget(QLabel('Historiske datasæt fra Zenodo og Dropbox'))
            self.list = QTreeWidget()
            self.list.setHeaderLabels(['Kategori / datasæt'])
            self.list.setUniformRowHeights(True)
            categories = {}
            for index, entry in enumerate(self.catalog):
                category = entry['category']
                if category not in categories:
                    parent = QTreeWidgetItem(self.list, [category])
                    parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    categories[category] = parent
                item = QTreeWidgetItem(categories[category], [entry['title']])
                item.setData(0, Qt.ItemDataRole.UserRole, index)
            self.list.expandAll()
            layout.addWidget(self.list)
            self.details = QLabel(); self.details.setWordWrap(True)
            layout.addWidget(self.details)
            self.progress = QProgressBar(); self.progress.setValue(0)
            layout.addWidget(self.progress)
            self.status = QLabel(); self.status.setWordWrap(True)
            layout.addWidget(self.status)
            row = QHBoxLayout(); layout.addLayout(row)
            self.load_button = QPushButton('Hent og tilføj til kort')
            self.source_button = QPushButton('Vis kilde')
            self.cancel_button = QPushButton('Annuller'); self.cancel_button.setEnabled(False)
            close = QPushButton('Luk')
            for btn in (self.load_button, self.source_button, self.cancel_button, close): row.addWidget(btn)
            self.load_button.clicked.connect(self.start)
            self.source_button.clicked.connect(self.open_source)
            self.cancel_button.clicked.connect(self.cancel)
            close.clicked.connect(d.hide)
            self.list.currentItemChanged.connect(self.select)
            self.list.setCurrentItem(self.list.topLevelItem(0).child(0))
        self.dialog.show(); self.dialog.raise_(); self.dialog.activateWindow()
    def entry(self):
        item = self.list.currentItem()
        index = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return self.catalog[index] if isinstance(index, int) else None
    def select(self, current, previous=None):
        entry = self.entry()
        self.load_button.setEnabled(entry is not None and self.task is None)
        self.source_button.setEnabled(entry is not None)
        if entry is None:
            self.details.clear(); self.status.clear(); self.progress.setValue(0)
            return
        folder = self.storage / str(entry['record_id'])
        self.details.setText(entry['description'] + ('\nDOI: ' + entry['doi'] if entry['doi'] else '\nKilde: Geoforum Danmark / Dropbox'))
        self.status.setText('Lokale filer findes.' if (folder / '.complete.json').exists() else 'Ikke hentet endnu.')
        self.progress.setValue(0)
    def open_source(self):
        entry = self.entry()
        if entry: QDesktopServices.openUrl(QUrl(entry.get('source_url') or 'https://zenodo.org/records/' + str(entry['record_id'])))
    def cancel(self):
        if self.task: self.task.cancel(); self.status.setText('Annullerer …')
    def start(self):
        entry = self.entry()
        if not entry or self.task: return
        self.load_button.setEnabled(False); self.list.setEnabled(False); self.cancel_button.setEnabled(True)
        self.status.setText('Kontrollerer lokale filer / henter fra kilden …')
        self.progress.setValue(0)
        self.task = DownloadTask(entry, self.storage / str(entry['record_id']), self.done)
        self.task.progressChanged.connect(lambda p: self.progress.setValue(int(p)))
        QgsApplication.taskManager().addTask(self.task)
    def done(self, task, ok):
        self.task = None
        self.load_button.setEnabled(True); self.list.setEnabled(True); self.cancel_button.setEnabled(False)
        if not ok:
            self.status.setText('Annulleret.' if task.isCanceled() else 'Fejl: ' + task.error)
            return
        project = QgsProject.instance(); root = project.layerTreeRoot()
        title = task.entry['title']; group = root.findGroup(title)
        existing = {layer.source() for layer in project.mapLayers().values()}
        count = 0; invalid = []
        for path in task.paths:
            match = re.search(r'\b(\d{4})\b', path.stem)
            name = 'Københavns matrikler ' + match.group(1) if match and task.entry['record_id'] == 24500156 else path.stem
            raster = path.suffix.lower() in ('.tif','.tiff','.img')
            sources = [(str(path), name)]
            if path.suffix.lower() in ('.gpkg','.gdb','.sqlite','.gml'):
                try:
                    details = QgsProviderRegistry.instance().providerMetadata('ogr').querySublayers(str(path))
                    if details: sources = [(x.uri(), x.name()) for x in details]
                except Exception as exc:
                    QgsMessageLog.logMessage(f"Kunne ikke læse underlag i {path}: {exc}", MENU, Qgis.MessageLevel.Warning)
            for uri, layer_name in sources:
                if uri in existing: continue
                layer = QgsRasterLayer(uri, layer_name) if raster else QgsVectorLayer(uri, layer_name, 'ogr')
                if not layer.isValid():
                    invalid.append(layer_name); continue
                if not raster: layer.setReadOnly(True)
                metadata = layer.metadata(); metadata.setTitle(layer_name); metadata.setIdentifier(task.entry['doi'] or task.entry.get('source_url','')); metadata.setAbstract(task.entry['description']); layer.setMetadata(metadata)
                if group is None: group = root.insertGroup(0, title)
                project.addMapLayer(layer, False); node = group.insertLayer(count, layer); node.setItemVisibilityChecked(False)
                existing.add(uri)
                count += 1
        if count and group:
            if group.parent() == root and root.children()[0] != group:
                clone = group.clone()
                root.insertChildNode(0, clone)
                root.removeChildNode(group)
                group = clone
            nodes = group.findLayers()
            if nodes:
                nodes[0].setItemVisibilityChecked(True)
                layer = nodes[0].layer()
                canvas = self.iface.mapCanvas()
                from qgis.core import QgsCoordinateTransform
                try:
                    extent = QgsCoordinateTransform(layer.crs(), canvas.mapSettings().destinationCrs(), project).transformBoundingBox(layer.extent())
                    extent.scale(1.08); canvas.setExtent(extent); canvas.refresh()
                except Exception as exc:
                    QgsMessageLog.logMessage(f"Kunne ikke zoome til {layer.name() if layer else title}: {exc}", MENU, Qgis.MessageLevel.Warning)
        self.status.setText(f'{count} lag tilføjet. ' + ('Kunne ikke åbne: ' + ', '.join(invalid) if invalid else 'Data er gemt lokalt.'))
