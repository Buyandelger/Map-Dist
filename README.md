# Element Map

Run this desktop app with Python 3.10 or newer and Tkinter. The standard Windows Python installer includes Tkinter. Native Excel import has an optional dependency.

```powershell
py app.py
```

## Manage Points, Elements, and values

1. Under **Points**, click **Add** and enter a name and coordinates as `latitude, longitude`. You can click the map to fill in the coordinates. Select a Point from the list or click its map marker.
2. Under **Elements**, click **Add** to name an Element at the selected Point. Select an Element to see its recorded values.
3. Under **Values**, click **Add** and enter a date and time in `YYYY-MM-DD HH:MM` format and a text or numeric value. Repeat for each reading.

Each list has **Edit** and **Delete** actions. You can also double-click a row to edit it. Editing a Point lets you change its name and coordinates. Editing an Element lets you change its name, and editing a value lets you change the reading and its date and time. Deleting a Point also deletes its Elements and values. Deleting an Element also deletes its values. Deletions require confirmation.

## Import a spreadsheet

Select **Import spreadsheet** and open a CSV or Excel `.xlsx` file. CSV works with the standard Python installation. To enable native Excel import, run `py -m pip install openpyxl` in the Python environment you use to launch the app. The importer reads the first worksheet and uses stored results for formula cells.

Use a header row with a **Name** column and one column per Element, such as **As**, **Cr**, or **Igeo As**. The **ID** column is ignored. Optional metadata columns include **Latitude** and **Longitude** or **Coordinates**, and **Date and time** or separate **Date** and **Time** columns. Use ISO dates and times. Date-only cells use midnight. Blank measurement cells and fully blank rows are skipped.

Review Point assignments in the preview. A uniquely matching Point name is suggested, and you can link any sample to a different Point. If the file has no coordinates, you can still import the sample and place the Point later using **Edit**. Points need coordinates to appear on the map or contribute to IDW. If a row has no timestamp, enter the shared collection date and time in the preview. Import skips readings with the same Point, Element, timestamp, and value.

## Navigate the map

Scroll up over the map to zoom in toward the cursor, and scroll down to zoom out. You can also use **Zoom in** and **Zoom out**. Hold the left mouse button and drag to move the map. A click without dragging still picks coordinates or selects a Point. The bottom and right scrollbars also pan the map. Select **Reset map view** to see the whole world again.

**Terrain** is on by default. It shows shaded elevation and, as you zoom in, contour lines at 1,000 m, 500 m, and 250 m intervals. Closer views load a finer elevation grid and more detailed rivers and lakes. Waterways stay visible when you turn Terrain off.

**Detailed online contours** adds 100 m, 50 m, 25 m, and 10 m contour intervals as the visible area becomes smaller. It is enabled by default and downloads public elevation tiles for the visible map area in the background. The elevation service receives tile coordinates. The offline contours remain available if the connection fails or you turn off detailed contours. Contour interval is the vertical spacing between lines, and the displayed grid spacing describes the horizontal spacing used to generate them.

**Detailed online waterways** is enabled by default. Zoom in to see lakes, reservoirs, rivers, and streams mapped in OpenStreetMap. The app downloads vector tiles for the visible area in the background. The offline water layer covers broader views and serves as a fallback if the connection fails or you turn off the online layer. Coverage depends on what has been mapped. [OpenFreeMap](https://openfreemap.org/) · [OpenMapTiles](https://openmaptiles.org/docs/schema/) · [OpenStreetMap copyright](https://www.openstreetmap.org/copyright)

## Calculate a distribution

1. Save the same numeric Element at two or more distinct Point locations. Use consistent units, such as °C for every Temperature entry.
2. Select a Point and an Element from the lists, then click **Calculate**.
3. Read the predicted values using the color range in the legend. Select **Clear distribution** to remove the overlay.

The calculation takes the latest numeric reading for that Element at each Point, matches names without regard to case, and averages readings at identical coordinates. It uses the nearest 12 distinct locations to the selected Point, including the selected location, with IDW power 2. Distances use a local approximation centered at the selected Point, so choose measurements in the same study area. The overlay is generated on demand.

For Esri's [ArcGIS Spatial Analyst IDW tool](https://pro.arcgis.com/en/pro-app/latest/tool-reference/spatial-analyst/idw.htm), run the app from the project folder with ArcGIS Pro and an available Spatial Analyst license. The ArcGIS Python environment needs Tkinter.

```powershell
& 'C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat' app.py
```

If ArcPy or the Spatial Analyst license is unavailable, the app uses its local Python IDW engine. The map legend identifies which engine generated the result.

## Storage and map data

The app creates `elements.sqlite3` in the project folder and reloads saved Points, Elements, and values on launch. Existing readings with the same Element name at a Point are grouped under one Element, ignoring differences in capitalization. When opening a database from the first version, the app also groups readings with identical coordinates under an automatically named Point.

The offline basemap uses public-domain Natural Earth country boundaries, populated places, rivers, and lakes. [Natural Earth cultural data](https://www.naturalearthdata.com/downloads/50m-cultural-vectors/) · [Natural Earth physical data](https://www.naturalearthdata.com/downloads/50m-physical-vectors/)

The outline of Бөөнцагаан нуур (Böön Tsagaan Lake) is bundled separately and appears in the offline map as you zoom in. Shoreline data © OpenStreetMap contributors, available under the ODbL. [Lake shoreline](https://www.openstreetmap.org/relation/1667570) · [OpenStreetMap copyright](https://www.openstreetmap.org/copyright)

The shaded terrain and contours use the 10-arc-minute GMT Earth Relief grid at wide zoom levels and the 6-arc-minute grid at closer zoom levels, both based on Scripps SRTM15+ v2.7 (Tozer et al., 2019). This resolution is suitable for regional terrain rather than individual slopes or local terraces. The waterways switch from Natural Earth 1:50m to 1:10m at closer zoom levels. [GMT Earth Relief](https://www.generic-mapping-tools.org/remote-datasets/earth-relief.html) · [Natural Earth detailed waterways](https://www.naturalearthdata.com/downloads/10m-physical-vectors/)

Online contours use Mapzen Terrain Tiles hosted by AWS. Available elevation resolution varies by location. [AWS Open Data](https://registry.opendata.aws/terrain-tiles/) · [Mapzen source attribution](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)
