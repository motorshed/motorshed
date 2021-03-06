# Old readme contents

( may still be useful, but may not be needed in main readme )


## You will also need to run the osrm-backend, either by OSRM.sh, or...
```
docker run -t -v $(pwd):/data osrm/osrm-backend osrm-extract -p /opt/car.lua /data/oregon-latest.osm.pbf
docker run -t -v $(pwd):/data osrm/osrm-backend osrm-partition /data/oregon-latest.osrm
docker run -t -v $(pwd):/data osrm/osrm-backend osrm-customize /data/oregon-latest.osrm
docker run -t -i -p 5000:5000 -v $(pwd):/data osrm/osrm-backend osrm-routed --algorithm mld /data/oregon-latest.osrm
```

### example:

```python
import motorshed
address = 'Astoria, OR'
G, center_node, origin_point = motorshed.get_map(address, distance=20000)
```

### or, you can run by place:
```python
place = 'Clatsop County, Oregon, USA'
G, center_node, origin_point = motorshed.get_map(address, place=place)
```


### then analyze and draw the map (5-70 it/s seems normal):
```python
motorshed.get_transit_times(G, origin_point)
missing_edges, missing_nodes = motorshed.find_all_routes(G, center_node)
motorshed.draw_map(G, center_node, color_by='through_traffic')
```

![alt text](images/Clatsop.png "Clatsop County")

### To animate, use convert:
```
convert -delay 10 -loop 0 2700*.svg.png animate-2700-by-time.gif
```
