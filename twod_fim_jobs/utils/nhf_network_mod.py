import logging
import os
from functools import cached_property

logger = logging.getLogger(__name__)

import geopandas as gpd
import pandas as pd
from consts import NHF_NETWORK_MODIFIER
from shapely import LineString, MultiLineString, MultiPoint, Point, Polygon
from shapely.ops import linemerge, substring


class NHFNetworkModifier:
    """Class to modify the NHF network data in a GeoPackage file.

    Assumes:
        The NHF network data is in a GeoPackage file with a layer named "flowpaths".
        The lakes data is in a GeoPackage file with a layer named "NHDWaterbody".
        The coastal data is in a GeoPackage file with a layer named "coastal" or a raster file.

    """

    def __init__(
        self,
        nhf_gpkg_path: str,
        lakes_gpkg_path: str,
        coastal_raster_path: str = None,
        coastal_gpkg_path: str = None,
        drainage_area_threshold_percent: float = 5,
        stream_order_threshold: int = 3,
        max_length_threshold_km: float = 3,
        lake_area_threshold_sqkm: float = 5,
        negative_lake_buffer_meters: float = 50,
    ):
        """Initialize the NHFNetworkModifier with the path to the GeoPackage file.

        Args:
            nhf_gpkg_path (str): Path to the NHF GeoPackage file..
            lakes_gpkg_path (str): Path to the lakes GeoPackage file.
            coastal_raster_path (str, optional): Path to the coastal raster file. Defaults to None.
            coastal_gpkg_path (str, optional): Path to the coastal GeoPackage file. Defaults to None.
            drainage_area_threshold_percent (float, optional): Drainage area threshold percentage. Defaults to 5.
            stream_order_threshold (int, optional): Stream order threshold. Defaults to 3.
            max_length_threshold_km (float, optional): Maximum length threshold in kilometers. Defaults to 8.
            lake_area_threshold_sqkm (float, optional): Lake area threshold in square kilometers. Defaults to 5.
            negative_lake_buffer_meters (float, optional): Negative buffer for lakes in meters. Defaults to 50.
        """
        self.nhf_gpkg_path = nhf_gpkg_path
        self.lakes_gpkg_path = lakes_gpkg_path
        self._coastal_gpkg_path = coastal_gpkg_path
        self.coastal_raster_path = coastal_raster_path
        self.drainage_area_threshold = drainage_area_threshold_percent
        self.stream_order_threshold = stream_order_threshold
        self.max_length_threshold_km = max_length_threshold_km
        self.lake_area_threshold_sqkm = lake_area_threshold_sqkm
        self.negative_lake_buffer_meters = negative_lake_buffer_meters

        for key, val in NHF_NETWORK_MODIFIER.items():
            setattr(self, key, val)
        self._new_reach_id = (
            self.nhf_gdf[self.reach_id].max() if not self.nhf_gdf.empty else 0
        )

    @property
    def coastal_gpkg_path(self):
        """Get the path to the coastal GeoPackage file."""
        if self._coastal_gpkg_path is not None:
            return self._coastal_gpkg_path
        else:
            if self.coastal_raster_path is not None:
                # If the coastal raster path is provided, derive the GeoPackage path from it
                return os.path.splitext(self.coastal_raster_path)[0] + ".gpkg"

    @cached_property
    def crs(self):
        """Get the coordinate reference system (CRS) of the NHF network data."""
        return self.nhf_gdf.crs

    @cached_property
    def coastal_gdf(self) -> gpd.GeoDataFrame:
        """Load the coastal data from the GeoPackage file."""
        if os.path.exists(self.coastal_gpkg_path):
            return gpd.read_file(self.coastal_gpkg_path).to_crs(self.crs)
        elif os.path.exists(self.coastal_raster_path):
            # If the coastal raster file exists, convert it to a GeoDataFrame
            # Implementation of raster to vector conversion goes here
            # For now, we will raise an error indicating that this is not implemented
            raise NotImplementedError(
                "Conversion from coastal raster to GeoDataFrame is not implemented."
            )
        else:
            raise FileNotFoundError(
                f"Neither coastal GeoPackage file nor coastal raster file found: {self.coastal_gpkg_path}, {self.coastal_raster_path}"
            )

    @cached_property
    def lakes_crs(self) -> gpd.GeoDataFrame:
        """Load the lakes data from the GeoPackage file."""
        if os.path.exists(self.lakes_gpkg_path):
            return gpd.read_file(self.lakes_gpkg_path, rows=1).crs
        else:
            raise FileNotFoundError(
                f"Lakes GeoPackage file not found: {self.lakes_gpkg_path}"
            )

    @cached_property
    def lakes_gdf(self) -> gpd.GeoDataFrame:
        """Load the lakes data from the GeoPackage file.

        processing steps:
        1. Read the lakes layer from the lakes GeoPackage file.
        2. Filter the lakes based on the lake area threshold.
        3. Perform a union of the lakes geometries to create a single geometry.
        4. Buffer the unioned geometry by the negative lake buffer in meters to create a buffer around the lakes.
        5. Explode the buffered geometry to create individual lake polygons.
        6. Filter the exploded geometries to keep only those with an area greater than the lake area threshold.

        Returns:
            gpd.GeoDataFrame: A GeoDataFrame containing the lakes data.

        """
        if os.path.exists(self.lakes_gpkg_path):
            # query = f"SELECT * FROM {self.lakes_layer} WHERE {self.lake_area_sqkm} > {self.lake_area_threshold_sqkm}"
            lakes_gdf = gpd.read_file(
                self.lakes_gpkg_path,
                bbox=tuple(self.nhf_gdf.to_crs(self.lakes_crs).total_bounds),
                layer=self.lakes_layer,
            ).to_crs(self.crs)

            lakes_gdf = gpd.GeoDataFrame(
                geometry=list(lakes_gdf.union_all().geoms), crs=self.crs
            )
            lakes_gdf["geometry"] = lakes_gdf.buffer(-self.negative_lake_buffer_meters)
            lakes_gdf = lakes_gdf.explode()
            lakes_gdf = lakes_gdf[lakes_gdf.area > self.lake_area_threshold_sqkm * 1e6]
            return lakes_gdf
        else:
            raise FileNotFoundError(
                f"Lakes GeoPackage file not found: {self.lakes_gpkg_path}"
            )

    @cached_property
    def nhf_gdf(self) -> gpd.GeoDataFrame:
        """Load the NHF network data from the GeoPackage file."""
        if os.path.exists(self.nhf_gpkg_path):
            query = f"SELECT * FROM {self.flowpaths_layer} WHERE {self.stream_order_field} >= {self.stream_order_threshold}"
            nhf_gdf = gpd.read_file(self.nhf_gpkg_path, sql=query)
            nhf_gdf["geometry"] = nhf_gdf.line_merge()
            return self.init_new_fields(nhf_gdf)
        else:
            raise FileNotFoundError(
                f"NHF GeoPackage file not found: {self.nhf_gpkg_path}"
            )

    def init_new_fields(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """Initialize new fields in the GeoDataFrame."""
        gdf[self.is_headwater] = False
        gdf[self.is_terminal] = False
        gdf[self.terminal_reason] = None
        gdf[self.waterbody_inlet["lake"]] = False
        gdf[self.waterbody_outlet["lake"]] = False
        gdf[self.waterbody_encompassed["lake"]] = False
        gdf[self.waterbody_inlet["coastal"]] = False
        gdf[self.waterbody_outlet["coastal"]] = False
        gdf[self.waterbody_encompassed["coastal"]] = False
        gdf[self.is_trimmed] = False
        gdf[self.reach_id] = gdf[self.fp_id]
        gdf[self.reach_to_id] = gdf[self.fp_to_id]
        return gdf

    def filter_by_stream_order(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """Filter the GeoDataFrame by stream order."""
        return gdf.loc[gdf[self.stream_order_field] >= self.stream_order_threshold]

    @property
    def waterbody_type(self):
        """Get the current waterbody type (either 'lake' or 'coastal')."""
        return self._waterbody_type

    @waterbody_type.setter
    def waterbody_type(self, value: str):
        """Set the current waterbody type (either 'lake' or 'coastal')."""
        if value not in ["lake", "coastal"]:
            raise ValueError("waterbody_type must be either 'lake' or 'coastal'")
        self._waterbody_type = value

    def merge_small_reaches(self, reach_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """Merge small reaches into their downstream neighbor.

        A reach is merged if:
        - Its length is less than max_length_threshold_km (3 km)
        - The combined length with its downstream reach is less than max_length_threshold_km
        - The drainage area difference with its downstream reach is less than drainage_area_threshold (5%)
        """
        logger.debug("Starting merge_small_reaches with %d reaches", len(reach_gdf))
        for _, reach_row in reach_gdf.iterrows():
            reach_id = reach_row[self.fp_id]
            reach_to_id = reach_row[self.fp_to_id]

            downstream = reach_gdf.loc[reach_gdf[self.fp_id] == reach_to_id]
            if downstream.empty:
                logger.debug("Reach %s: no downstream reach found, skipping", reach_id)
                continue

            downstream_row = downstream.iloc[0]
            area_pct = abs(
                (
                    (reach_row[self.area_sqkm] - downstream_row[self.area_sqkm])
                    / reach_row[self.area_sqkm]
                )
                * 100
            )
            new_length = reach_row[self.length_km] + downstream_row[self.length_km]

            if (
                area_pct < self.drainage_area_threshold
                and new_length <= self.max_length_threshold_km
            ):
                logger.debug(
                    "Reach %s: merging with downstream reach %s", reach_id, reach_to_id
                )
                cond = (reach_gdf[self.fp_id] == reach_id) | (
                    reach_gdf[self.fp_id] == reach_to_id
                )
                selected_gdf = reach_gdf[cond]
                reach_gdf = reach_gdf[~cond]
                selected_gdf = selected_gdf.dissolve()
                reach_gdf = pd.concat([selected_gdf, reach_gdf]).reset_index(drop=True)

        logger.debug("Finished merge_small_reaches with %d reaches", len(reach_gdf))
        return reach_gdf

    def identify_terminal_reaches(
        self, reach_gdf: gpd.GeoDataFrame
    ) -> gpd.GeoDataFrame:
        """Identify terminal reaches in the network."""
        reach_gdf.loc[reach_gdf[self.fp_to_id].isnull(), self.is_terminal] = True
        reach_gdf.loc[reach_gdf[self.fp_to_id].isnull(), self.terminal_reason] = (
            "outlet"
        )
        return reach_gdf

    def identify_headwater_reaches(
        self, reach_gdf: gpd.GeoDataFrame
    ) -> gpd.GeoDataFrame:
        """Identify headwater reaches in the network."""
        fp_ids = reach_gdf[self.fp_id]
        reach_gdf.loc[~reach_gdf[self.fp_to_id].isin(fp_ids), self.is_headwater] = True
        return reach_gdf

    def handle_flat_reaches(self, reach_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """Handle flat reaches in the network."""
        # Implementation of handling flat reaches goes here
        return reach_gdf

    def identify_reaches_associated_with_waterbodies(
        self, reach_gdf: gpd.GeoDataFrame, waterbodies_gdf: gpd.GeoDataFrame
    ) -> gpd.GeoDataFrame:
        """Identify reaches that are associated with waterbodies."""
        return reach_gdf.loc[reach_gdf.intersects(waterbodies_gdf.union_all())]

    def identify_inlet_outlet_reaches(
        self, reach_gdf: gpd.GeoDataFrame, waterbodies_gdf: gpd.GeoDataFrame
    ) -> gpd.GeoDataFrame:
        """Identify reaches that are inlets or outlets of lakes."""
        waterbody_reaches = self.identify_reaches_associated_with_waterbodies(
            reach_gdf, waterbodies_gdf
        )
        for _, reach_row in waterbody_reaches.iterrows():
            reach_id = reach_row[self.reach_id]
            # Identify the waterbody polygon(s) associated with the reach
            waterbody_polygons = self.identify_waterbody_polygons_for_reach(
                reach_row, waterbodies_gdf
            )
            if waterbody_polygons is None:
                continue

            # if only one waterbody polygon, check if reach is complete within it
            if len(waterbody_polygons) == 1 and reach_row.geometry.within(
                waterbody_polygons.iloc[0]
            ):
                reach_gdf.loc[
                    reach_gdf[self.reach_id] == reach_id,
                    self.waterbody_encompassed[self.waterbody_type],
                ] = True
                continue

            # there can be multiple waterbody polygons for a single reach; need to iterate through them
            for waterbody_polygon in waterbody_polygons:
                # Determine the intersection points between the reach and the waterbody polygon
                intersection_points = self.detemine_intersectiion_points(
                    reach_row, waterbody_polygon
                )

                # process reaches that intersect the waterbody polygon at a single point
                reach_gdf = self.update_reach_gdf_with_split_reaches(
                    reach_row,
                    waterbody_polygon,
                    reach_gdf,
                    reach_id,
                    intersection_points,
                )
        return reach_gdf

    def determine_waterbody_order_along_reach(
        self, reach_row: pd.Series, waterbody_polygons: gpd.GeoSeries
    ) -> gpd.GeoSeries:
        """Determine the order of waterbodies along a reach."""
        for _, waterbody_row in waterbody_polygons.iterrows():
            waterbody_polygon = waterbody_row.geometry
            # Determine the intersection points between the reach and the waterbody polygon
            intersection_points = self.detemine_intersectiion_points(
                reach_row, waterbody_polygon
            )
            # Sort the intersection points along the reach geometry
            sorted_points = sorted(
                intersection_points.geoms,
                key=lambda p: reach_row.geometry.project(p),
            )
            # Store the sorted points in a new column in the waterbody_polygons GeoSeries
            waterbody_polygons.loc[
                waterbody_polygons.index == waterbody_row.name, "sorted_points"
            ] = [sorted_points]
        return waterbody_polygons

    @property
    def new_reach_id(self) -> int:
        """Generate a new reach ID based on the maximum existing reach ID."""
        self._new_reach_id += 1
        return self._new_reach_id

    def modify_reach_gdf_for_inlet_reach(
        self,
        reach_row: pd.Series,
        reach_gdf: gpd.GeoDataFrame,
        reach_id: int,
        intersection_point: Point | MultiPoint,
    ):
        """Modify the reach GeoDataFrame for an inlet reach.

        An inlet reach is a reach whose downstream end is within the waterbody polygon
        """
        reach_gdf.loc[
            reach_gdf[self.reach_id] == reach_id,
            self.waterbody_inlet[self.waterbody_type],
        ] = True
        reach_gdf.loc[reach_gdf[self.reach_id] == reach_id, self.is_terminal] = True
        reach_gdf.loc[reach_gdf[self.reach_id] == reach_id, self.reach_to_id] = None
        reach_gdf.loc[reach_gdf[self.reach_id] == reach_id, self.terminal_reason] = (
            self.waterbody_inlet[self.waterbody_type]
        )
        reach_gdf.loc[reach_gdf[self.reach_id] == reach_id, self.geometry_field] = (
            self.trim_reach_at_point(reach_row.geometry, intersection_point, "inlet")
        )
        return reach_gdf

    def modify_reach_gdf_for_outlet_reach(
        self,
        reach_row: pd.Series,
        reach_gdf: gpd.GeoDataFrame,
        reach_id: int,
        intersection_point: Point | MultiPoint,
    ):
        """Modify the reach GeoDataFrame for an outlet reach.

        An outlet reach is a reach whose upstream end is within the waterbody polygon.
        """
        reach_gdf.loc[
            reach_gdf[self.reach_id] == reach_id,
            self.waterbody_outlet[self.waterbody_type],
        ] = True
        reach_gdf.loc[reach_gdf[self.reach_id] == reach_id, self.is_headwater] = True
        reach_gdf.loc[reach_gdf[self.reach_id] == reach_id, self.geometry_field] = (
            self.trim_reach_at_point(reach_row.geometry, intersection_point, "outlet")
        )
        return reach_gdf

    def modify_reach_gdf_for_both_inlet_and_outlet_reach(
        self,
        reach_row: pd.Series,
        reach_gdf: gpd.GeoDataFrame,
        reach_id: int,
        intersection_points: Point | MultiPoint,
    ):
        """Modify the reach GeoDataFrame for a reach that is both an inlet and an outlet.

        If the reach's upstream or downstream end is not within the waterbody polygon
        and is not encompassed by the waterbody polygon, then the reach needs to be
        trimmed at the intersection points with the waterbody polygon and will be both
        an inlet and an outlet reach. The creation of a new reach is necessary to maintain
        the network connectivity and ensure that the reach is properly represented in the modified network.
        """
        # add new row for the outlet reach
        outlet_row = reach_row.copy()
        outlet_row[self.reach_id] = self.new_reach_id
        outlet_gdf = gpd.GeoDataFrame(
            outlet_row.to_frame().T, geometry=self.geometry_field, crs=self.crs
        )
        reach_gdf = pd.concat(
            [
                reach_gdf,
                outlet_gdf,
            ],
            ignore_index=True,
        )

        reach_gdf = self.modify_reach_gdf_for_inlet_reach(
            reach_row, reach_gdf, reach_id, intersection_points.geoms[0]
        )
        reach_gdf = self.modify_reach_gdf_for_outlet_reach(
            outlet_row, reach_gdf, self.new_reach_id - 1, intersection_points.geoms[-1]
        )
        return reach_gdf

    def update_reach_gdf_with_split_reaches(
        self,
        reach_row: pd.Series,
        waterbody_polygon: Polygon,
        reach_gdf: gpd.GeoDataFrame,
        reach_id: int,
        intersection_points: Point | MultiPoint,
    ):
        """Update the reach GeoDataFrame with split reaches based on intersection points.

        Split options are:
        1. If the reach's downstream end is within the waterbody polygon, it is an inlet reach (after splitting in 2 and taking the upstream segment).
        2. If the reach's upstream end is within the waterbody polygon, it is an outlet reach (after splitting in 2 and taking the downstream segment).
        3. If the reach's upstream and downstream ends are not within the waterbody polygon, it is both an inlet and an outlet reach (after splitting in 3 and taking the outer segments).
        """
        if reach_row.geometry.boundary.geoms[1].within(waterbody_polygon):
            if isinstance(intersection_points, MultiPoint):
                intersection_points = intersection_points.geoms[0]
            reach_gdf = self.modify_reach_gdf_for_inlet_reach(
                reach_row, reach_gdf, reach_id, intersection_points
            )
        elif reach_row.geometry.boundary.geoms[0].within(waterbody_polygon):
            if isinstance(intersection_points, MultiPoint):
                intersection_points = intersection_points.geoms[-1]
            reach_gdf = self.modify_reach_gdf_for_outlet_reach(
                reach_row, reach_gdf, reach_id, intersection_points
            )
        else:
            reach_gdf = self.modify_reach_gdf_for_both_inlet_and_outlet_reach(
                reach_row, reach_gdf, reach_id, intersection_points
            )
        return reach_gdf

    def trim_reach_at_point(
        self, reach_geom: LineString, intersection_point: Point, tag: str
    ) -> LineString:
        """Trim the reach geometry at the intersection point with the waterbody polygon."""
        if tag == "inlet":
            return substring(reach_geom, 0, reach_geom.project(intersection_point))
        elif tag == "outlet":
            return substring(
                reach_geom, reach_geom.project(intersection_point), reach_geom.length
            )
        else:
            raise ValueError(f"Invalid tag for trimming reach: {tag}")

    def detemine_intersectiion_points(
        self, reach_row: pd.Series, waterbody_polygon: Polygon
    ) -> Point | MultiPoint:
        """Determine the intersection points between a reach and a waterbody polygon."""
        intersect_points = reach_row.geometry.intersection(waterbody_polygon.boundary)
        if not isinstance(intersect_points, (Point, MultiPoint)):
            raise ValueError(
                f"Intersection is not a Point or MultiPoint: {intersect_points}"
            )
        return intersect_points

    def identify_waterbody_polygons_for_reach(
        self, reach_row: pd.Series, waterbodies_gdf: gpd.GeoDataFrame
    ) -> gpd.GeoSeries | None:
        """Identify the waterbody polygon associated with a given reach."""
        if waterbodies_gdf.empty:
            return None
        elif len(waterbodies_gdf) == 1:
            return waterbodies_gdf.geometry
        return waterbodies_gdf.loc[
            waterbodies_gdf.intersects(reach_row.geometry), "geometry"
        ]

    def adjust_reaches_at_waterbodies(
        self, reach_gdf: gpd.GeoDataFrame, waterbodies_gdf: gpd.GeoDataFrame
    ) -> gpd.GeoDataFrame:
        """Adjust reaches that are adjacent to waterbodies."""
        reach_gdf = self.identify_inlet_outlet_reaches(reach_gdf, waterbodies_gdf)
        reach_gdf.drop(
            reach_gdf[reach_gdf[self.waterbody_encompassed["coastal"]]].index,
            inplace=True,
        )
        reach_gdf.drop(
            reach_gdf[reach_gdf[self.waterbody_encompassed["lake"]]].index,
            inplace=True,
        )
        return reach_gdf

    def save_modified_network(self, gdf: gpd.GeoDataFrame, output_gpkg_path: str):
        """Save the modified GeoDataFrame to a new GeoPackage file."""
        gdf.to_file(output_gpkg_path, layer=self.flowpaths_layer, driver="GPKG")
        self.lakes_gdf.to_file(output_gpkg_path, layer=self.lakes_layer, driver="GPKG")

    def modify_network(self, output_gpkg_path: str):
        """Modify the NHF network data and save it to a new GeoPackage file.

        Process steps:
        1. Identify terminal reaches in the network (reaches with null fp_to_id).
        2. Identify headwater reaches in the network (reaches with stream order == stream_order_threshold).
        3. Filter reaches by stream order based on stream order >= stream_order_threshold.
        4. Adjust reaches that are adjacent to coastal waterbodies.
        5. Adjust reaches that are adjacent to lake waterbodies.
        6. Merge small reaches that are shorter than the maximum length threshold.
        7. Save the modified network to a new GeoPackage file.

        Args:
            output_gpkg_path (str): Path to the output GeoPackage file.

        """
        nhf_gdf = self.nhf_gdf.copy()
        nhf_gdf = self.identify_terminal_reaches(nhf_gdf)
        nhf_gdf = self.filter_by_stream_order(nhf_gdf)
        nhf_gdf = self.identify_headwater_reaches(nhf_gdf)
        self.waterbody_type = "coastal"
        nhf_gdf = self.adjust_reaches_at_waterbodies(nhf_gdf, self.coastal_gdf)
        self.waterbody_type = "lake"
        nhf_gdf = self.adjust_reaches_at_waterbodies(nhf_gdf, self.lakes_gdf)
        nhf_gdf = self.merge_small_reaches(nhf_gdf)
        self.save_modified_network(nhf_gdf, output_gpkg_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    coastal_gpkg_path = "/mnt/d/NOAA/FIM/Data/MHHW.gpkg"
    nhf_gpkg_path = "/mnt/d/NOAA/FIM/Data/nhf_test.gpkg"
    lakes_gpkg_path = "/mnt/d/NOAA/FIM/Data/NHD_H_Louisiana_State_GPKG.gpkg"

    nm = NHFNetworkModifier(
        nhf_gpkg_path,
        lakes_gpkg_path,
        coastal_raster_path=coastal_gpkg_path,
    )

    nm.modify_network(output_gpkg_path="/mnt/d/NOAA/FIM/Data/NHF_modified.gpkg")
