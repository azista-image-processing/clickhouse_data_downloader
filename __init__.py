def classFactory(iface):
    from .plugin import ClickhouseDownloader
    return ClickhouseDownloader(iface)
