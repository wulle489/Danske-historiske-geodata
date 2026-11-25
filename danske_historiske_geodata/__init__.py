def classFactory(iface):
    from .plugin import HistoricalDataPlugin
    return HistoricalDataPlugin(iface)
