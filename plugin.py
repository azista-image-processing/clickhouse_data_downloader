import csv
import os
import subprocess
import sys
import tempfile
import time

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsProject, QgsTask, QgsVectorLayer)
from qgis.gui import QgsExtentWidget
from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (QAction, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
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


HELP = "AOI placeholders (EPSG:4326): {min_lon} {min_lat} {max_lon} {max_lat}"


def _q(name):
    """Backtick-quote a ClickHouse identifier."""
    return '`' + name.replace('`', '``') + '`'


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
        self.client = None
        self.setWindowTitle('ClickHouse Data Downloader')
        self.resize(560, 780)
        self.host, self.port, self.user, self.password = QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.save_creds = QCheckBox('Save Database Credentials')
        self.connect_btn = QPushButton('Connect')
        self.connect_btn.clicked.connect(self.connect_db)
        self.database, self.table, self.lon, self.lat = QComboBox(), QComboBox(), QComboBox(), QComboBox()
        self.database.currentIndexChanged.connect(self.load_tables)
        self.table.currentIndexChanged.connect(self.load_columns)
        for c in (self.lon, self.lat):
            c.currentIndexChanged.connect(self.reset_query)

        # Same native extent widget (with its dropdown menu) as the Processing tools
        self.aoi = QgsExtentWidget(None, QgsExtentWidget.CondensedStyle)
        self.aoi.setNullValueAllowed(True, 'Not set (download everything)')
        self.aoi.setMapCanvas(self.iface.mapCanvas())
        self.aoi.setOutputCrs(QgsProject.instance().crs())

        self.sql = QPlainTextEdit()
        reset = QPushButton('Reset Query')
        reset.clicked.connect(self.reset_query)
        self.out = QLineEdit()
        browse = QPushButton('...')
        browse.clicked.connect(self.browse)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out)
        out_row.addWidget(browse)
        run = QPushButton('Download && Add Layer')
        run.clicked.connect(self.run)

        lay = QVBoxLayout(self)
        for label, w in (('Enter ClickHouse Host', self.host), ('Enter ClickHouse Port', self.port),
                         ('Username', self.user), ('Password', self.password)):
            lay.addWidget(QLabel(label))
            lay.addWidget(w)
        row = QHBoxLayout()
        row.addWidget(self.save_creds)
        row.addWidget(self.connect_btn)
        lay.addLayout(row)
        for label, w in (('Select Database', self.database), ('Select Table', self.table),
                         ('Select Longitude Column (numeric)', self.lon), ('Select Latitude Column (numeric)', self.lat),
                         ('Extent (AOI)', self.aoi)):
            lay.addWidget(QLabel(label))
            lay.addWidget(w)
        lay.addWidget(QLabel('Custom SQL Query - ' + HELP))
        lay.addWidget(self.sql)
        lay.addWidget(reset)
        lay.addWidget(QLabel('Output CSV (optional, leave empty for a temp file)'))
        lay.addLayout(out_row)
        lay.addWidget(run)
        self.load_credentials()

    def load_credentials(self):
        s = QSettings()
        self.host.setText(s.value('ch_dl/host', 'localhost'))
        self.port.setText(s.value('ch_dl/port', '8123'))
        self.user.setText(s.value('ch_dl/user', 'default'))
        self.password.setText(s.value('ch_dl/password', ''))
        self.save_creds.setChecked(s.value('ch_dl/save', False, type=bool))

    def save_credentials(self):
        s = QSettings()
        keep = self.save_creds.isChecked()
        s.setValue('ch_dl/save', keep)
        # ponytail: password stored in plain QSettings (like the connector's credentials.json)
        for k, w in (('host', self.host), ('port', self.port), ('user', self.user), ('password', self.password)):
            s.setValue('ch_dl/' + k, w.text() if keep else '')

    def conn_params(self):
        return dict(host=self.host.text(), port=int(self.port.text()), username=self.user.text(),
                    password=self.password.text())

    def connect_db(self):
        try:
            self.client = _clickhouse_connect().get_client(**self.conn_params())
            dbs = [r[0] for r in self.client.query('SHOW DATABASES').result_rows]
        except Exception as e:
            return QMessageBox.critical(self, 'Connection Error', f'Failed to connect to ClickHouse: {e}')
        self.save_credentials()
        self.database.clear()
        self.database.addItems(dbs)
        QMessageBox.information(self, 'Connection Successful', 'Connected to ClickHouse successfully!')

    def load_tables(self):
        self.table.clear()
        if self.client and self.database.currentText():
            rows = self.client.query(f'SHOW TABLES FROM {_q(self.database.currentText())}').result_rows
            self.table.addItems([r[0] for r in rows])

    def load_columns(self):
        for c in (self.lon, self.lat):
            c.blockSignals(True)
            c.clear()
        if self.client and self.table.currentText():
            cols = [r[0] for r in self.client.query(
                f'DESCRIBE TABLE {_q(self.database.currentText())}.{_q(self.table.currentText())}').result_rows]
            for c, hints in ((self.lon, ('lon', 'lng')), (self.lat, ('lat',))):
                c.addItems(cols)
                # preselect the first column whose name looks like lon/lat
                c.setCurrentIndex(next((i for i, n in enumerate(cols) if any(h in n.lower() for h in hints)), 0))
        for c in (self.lon, self.lat):
            c.blockSignals(False)
        self.reset_query()

    def reset_query(self):
        if not self.table.currentText():
            return
        lon, lat = _q(self.lon.currentText()), _q(self.lat.currentText())
        self.sql.setPlainText(
            f'SELECT *\nFROM {_q(self.database.currentText())}.{_q(self.table.currentText())}\n'
            f'WHERE {lon} BETWEEN {{min_lon}} AND {{max_lon}}\n  AND {lat} BETWEEN {{min_lat}} AND {{max_lat}}\n'
            f'LIMIT 100000')

    def browse(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save CSV', self.out.text(), 'CSV (*.csv)')
        if path:
            self.out.setText(path if path.lower().endswith('.csv') else path + '.csv')

    def run(self):
        sql, path = self.sql.toPlainText().strip().rstrip(';'), self.out.text().strip()
        if not sql:
            return QMessageBox.warning(self, 'Missing input', 'Enter a query.')
        if not path:  # no path given: write to a temp file
            name = f"{self.table.currentText() or 'clickhouse'}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            path = os.path.join(tempfile.gettempdir(), name)
        extent = self.aoi.outputExtent()
        if not extent.isNull():
            r = QgsCoordinateTransform(self.aoi.outputCrs(), QgsCoordinateReferenceSystem('EPSG:4326'),
                                       QgsProject.instance()).transformBoundingBox(extent)
            for k, v in (('min_lon', r.xMinimum()), ('min_lat', r.yMinimum()),
                         ('max_lon', r.xMaximum()), ('max_lat', r.yMaximum())):
                sql = sql.replace('{%s}' % k, repr(v))
        elif '{min_' in sql or '{max_' in sql:
            return QMessageBox.warning(self, 'No AOI', 'Query uses AOI placeholders but no extent is set.')
        try:
            conn = dict(self.conn_params(), database=self.database.currentText() or 'default')
        except ValueError:
            return QMessageBox.warning(self, 'Bad port', 'Port must be a number.')
        x, y = self.lon.currentText(), self.lat.currentText()

        def work(task):
            return download_csv(conn, sql, path)

        def done(exc, rows=None):
            if exc:
                return self.iface.messageBar().pushMessage('ClickHouse download failed', str(exc), Qgis.Critical)
            self.add_layer(path, x, y)
            self.iface.messageBar().pushMessage('ClickHouse', f'{rows} rows saved to {path}', Qgis.Success)

        # keep a reference or the task gets garbage collected
        self.task = QgsTask.fromFunction('ClickHouse download', work, on_finished=done)
        QgsApplication.taskManager().addTask(self.task)
        self.iface.messageBar().pushMessage('ClickHouse', 'Download started...', Qgis.Info)

    def add_layer(self, path, x, y):
        with open(path, encoding='utf-8') as f:
            cols = next(csv.reader(f), [])
        uri = 'file:///' + path.replace('\\', '/') + '?delimiter=,'
        # plot as points when the chosen lon/lat columns are in the result, otherwise a plain table
        uri += f'&xField={x}&yField={y}&crs=EPSG:4326' if x in cols and y in cols else '&geomType=none'
        layer = QgsVectorLayer(uri, os.path.splitext(os.path.basename(path))[0], 'delimitedtext')
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)


class ClickhouseDownloader:
    def __init__(self, iface):
        self.iface = iface
        self.dlg = None

    def initGui(self):
        icon = QgsApplication.getThemeIcon('/mActionAddOgrLayer.svg')
        self.action = QAction(icon, 'ClickHouse Data Downloader', self.iface.mainWindow())
        self.action.triggered.connect(self.show)
        # toolbar button + Plugins menu (the Database menu can be hidden in some profiles)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('&ClickHouse Data Downloader', self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu('&ClickHouse Data Downloader', self.action)

    def show(self):
        if self.dlg is None:
            self.dlg = Dialog(self.iface)
        self.dlg.show()
        self.dlg.raise_()
