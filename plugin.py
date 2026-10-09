import csv
import os
import subprocess
import sys

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsProject, QgsTask, QgsVectorLayer)
from qgis.gui import QgsExtentGroupBox
from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (QAction, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                                 QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

# clickhouse_connect is installed on first use into ./libs, same approach as the clickhouse_connector plugin
LIBS = os.path.join(os.path.dirname(__file__), 'libs')
sys.path.append(LIBS)


def _clickhouse_connect():
    try:
        import clickhouse_connect
    except ImportError:
        os.makedirs(LIBS, exist_ok=True)
        # ponytail: sys.executable is qgis-bin on Windows, so use the bundled python for pip
        py = os.path.join(sys.prefix, 'python.exe') if os.name == 'nt' else sys.executable
        subprocess.check_call([py, '-m', 'pip', 'install', '--target', LIBS, 'clickhouse-connect'])
        import clickhouse_connect
    return clickhouse_connect


DEFAULT_SQL = """SELECT mmsi, position_utc, lat, lon, speed, course
FROM ais_positions
WHERE lon BETWEEN {min_lon} AND {max_lon}
  AND lat BETWEEN {min_lat} AND {max_lat}
LIMIT 100000"""

HELP = "AOI placeholders (EPSG:4326): {min_lon} {min_lat} {max_lon} {max_lat}"


def download_csv(conn, sql, path):
    """Stream a query result to CSV. Runs in a worker thread, so it opens its own client."""
    client = _clickhouse_connect().get_client(**conn)
    rows = 0
    with open(path, 'w', newline='', encoding='utf-8') as f, \
            client.query_row_block_stream(sql) as stream:
        w = csv.writer(f)
        w.writerow(stream.source.column_names)
        for block in stream:
            w.writerows(block)
            rows += len(block)
    return rows


class Dialog(QDialog):
    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.setWindowTitle('ClickHouse Data Downloader')
        self.resize(640, 700)
        s = QSettings()
        self.host = QLineEdit(s.value('ch_dl/host', 'localhost'))
        self.port = QLineEdit(s.value('ch_dl/port', '8123'))
        self.user = QLineEdit(s.value('ch_dl/user', 'default'))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.database = QLineEdit(s.value('ch_dl/db', 'default'))

        form = QFormLayout()
        for label, w in (('Host', self.host), ('Port', self.port), ('Username', self.user),
                         ('Password', self.password), ('Database', self.database)):
            form.addRow(label, w)

        # Native QGIS AOI widget: draw on canvas / map canvas extent / layer extent / bookmark
        self.aoi = QgsExtentGroupBox()
        self.aoi.setTitle('AOI (optional)')
        self.aoi.setCheckable(True)
        self.aoi.setChecked(False)
        self.aoi.setMapCanvas(iface.mapCanvas())
        self.aoi.setOutputCrs(QgsProject.instance().crs())

        self.sql = QPlainTextEdit(DEFAULT_SQL)
        self.out = QLineEdit()
        browse = QPushButton('...')
        browse.clicked.connect(self.browse)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out)
        out_row.addWidget(browse)
        run = QPushButton('Download && Add Layer')
        run.clicked.connect(self.run)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.aoi)
        lay.addWidget(QLabel('SQL query - ' + HELP))
        lay.addWidget(self.sql)
        lay.addWidget(QLabel('Output CSV'))
        lay.addLayout(out_row)
        lay.addWidget(run)

    def browse(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save CSV', self.out.text(), 'CSV (*.csv)')
        if path:
            self.out.setText(path if path.lower().endswith('.csv') else path + '.csv')

    def run(self):
        sql, path = self.sql.toPlainText().strip().rstrip(';'), self.out.text().strip()
        if not sql or not path:
            return QMessageBox.warning(self, 'Missing input', 'Enter a query and an output file.')
        if self.aoi.isChecked():
            r = QgsCoordinateTransform(self.aoi.currentCrs(), QgsCoordinateReferenceSystem('EPSG:4326'),
                                       QgsProject.instance()).transformBoundingBox(self.aoi.currentExtent())
            for k, v in (('min_lon', r.xMinimum()), ('min_lat', r.yMinimum()),
                         ('max_lon', r.xMaximum()), ('max_lat', r.yMaximum())):
                sql = sql.replace('{%s}' % k, repr(v))
        elif '{min_' in sql or '{max_' in sql:
            return QMessageBox.warning(self, 'No AOI', 'Query uses AOI placeholders but AOI is not enabled.')

        s = QSettings()
        for k, w in (('host', self.host), ('port', self.port), ('user', self.user), ('db', self.database)):
            s.setValue('ch_dl/' + k, w.text())
        try:
            conn = dict(host=self.host.text(), port=int(self.port.text()), username=self.user.text(),
                        password=self.password.text(), database=self.database.text())
        except ValueError:
            return QMessageBox.warning(self, 'Bad port', 'Port must be a number.')

        def work(task):
            return download_csv(conn, sql, path)

        def done(exc, rows=None):
            if exc:
                return self.iface.messageBar().pushMessage('ClickHouse download failed', str(exc), Qgis.Critical)
            self.add_layer(path)
            self.iface.messageBar().pushMessage('ClickHouse', f'{rows} rows saved to {path}', Qgis.Success)

        # keep a reference or the task gets garbage collected
        self.task = QgsTask.fromFunction('ClickHouse download', work, on_finished=done)
        QgsApplication.taskManager().addTask(self.task)
        self.iface.messageBar().pushMessage('ClickHouse', 'Download started...', Qgis.Info)

    def add_layer(self, path):
        with open(path, encoding='utf-8') as f:
            cols = next(csv.reader(f), [])
        lower = {c.lower(): c for c in cols}
        x = lower.get('lon') or lower.get('longitude')
        y = lower.get('lat') or lower.get('latitude')
        uri = 'file:///' + path.replace('\\', '/') + '?delimiter=,'
        # plot as points when coordinate columns exist, otherwise a plain table
        uri += f'&xField={x}&yField={y}&crs=EPSG:4326' if x and y else '&geomType=none'
        layer = QgsVectorLayer(uri, os.path.splitext(os.path.basename(path))[0], 'delimitedtext')
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)


class ClickhouseDownloader:
    def __init__(self, iface):
        self.iface = iface
        self.dlg = None

    def initGui(self):
        self.action = QAction('ClickHouse Data Downloader', self.iface.mainWindow())
        self.action.triggered.connect(self.show)
        self.iface.addPluginToDatabaseMenu('&ClickHouse Data Downloader', self.action)

    def unload(self):
        self.iface.removePluginDatabaseMenu('&ClickHouse Data Downloader', self.action)

    def show(self):
        if self.dlg is None:
            self.dlg = Dialog(self.iface)
        self.dlg.show()
        self.dlg.raise_()
