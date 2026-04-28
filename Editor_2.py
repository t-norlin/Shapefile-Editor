import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import streamlit as st
from folium import GeoJson, GeoJsonPopup, GeoJsonTooltip, LayerControl, Map
from folium.plugins import Draw
from shapely.geometry import shape
from streamlit_folium import st_folium

st.set_page_config(page_title="Shapefile Geometry Editor", layout="wide")


# =========================
# Helpers
# =========================
def extract_zip_to_temp(uploaded_file):
    temp_dir = tempfile.mkdtemp(prefix="shp_edit_")
    zip_path = os.path.join(temp_dir, "uploaded.zip")
    with open(zip_path, "wb") as f:
        f.write(uploaded_file.getbuffer())

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(temp_dir)

    shp_files = list(Path(temp_dir).rglob("*.shp"))
    if not shp_files:
        raise FileNotFoundError("No .shp file found in the uploaded ZIP.")

    return temp_dir, str(shp_files[0])


@st.cache_data(show_spinner=False)
def load_shapefile_from_zip(file_bytes, filename_hint):
    # Cache-friendly wrapper using raw bytes instead of UploadedFile object
    temp_dir = tempfile.mkdtemp(prefix="shp_cache_")
    zip_path = os.path.join(temp_dir, filename_hint)
    with open(zip_path, "wb") as f:
        f.write(file_bytes)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(temp_dir)

    shp_files = list(Path(temp_dir).rglob("*.shp"))
    if not shp_files:
        raise FileNotFoundError("No .shp file found in the uploaded ZIP.")

    shp_path = str(shp_files[0])
    gdf = gpd.read_file(shp_path)
    return gdf, shp_path, temp_dir


def ensure_wgs84(gdf):
    if gdf.crs is None:
        raise ValueError("The shapefile has no CRS defined. Please add a .prj file or set a CRS before using this app.")
    return gdf.to_crs(epsg=4326)



def make_map(gdf_wgs84, selected_idx=None, id_field=None):
    if gdf_wgs84.empty:
        return Map(location=[0, 0], zoom_start=2)

    bounds = gdf_wgs84.total_bounds  # minx, miny, maxx, maxy
    center_lat = (bounds[1] + bounds[3]) / 2
    center_lon = (bounds[0] + bounds[2]) / 2

    m = Map(location=[center_lat, center_lon], zoom_start=10, tiles="OpenStreetMap")

    # All features layer
    tooltip_fields = []
    if id_field and id_field in gdf_wgs84.columns:
        tooltip_fields.append(id_field)
    tooltip_fields.append("_fid")

    GeoJson(
        data=json.loads(gdf_wgs84.to_json()),
        name="All features",
        tooltip=GeoJsonTooltip(fields=[f for f in tooltip_fields if f in gdf_wgs84.columns], aliases=[f for f in tooltip_fields if f in gdf_wgs84.columns]),
        popup=GeoJsonPopup(fields=[f for f in tooltip_fields if f in gdf_wgs84.columns]),
        style_function=lambda x: {
            "color": "#2563eb",
            "weight": 2,
            "fillColor": "#60a5fa",
            "fillOpacity": 0.15,
        },
    ).add_to(m)

    if selected_idx is not None:
        selected = gdf_wgs84.loc[[selected_idx]]
        GeoJson(
            data=json.loads(selected.to_json()),
            name="Selected feature",
            style_function=lambda x: {
                "color": "#dc2626",
                "weight": 4,
                "fillColor": "#f87171",
                "fillOpacity": 0.25,
            },
        ).add_to(m)

    draw = Draw(
        export=False,
        draw_options={
            "polyline": True,
            "polygon": True,
            "rectangle": True,
            "circle": False,
            "circlemarker": False,
            "marker": True,
        },
        edit_options={
            "edit": False,
            "remove": True,
        },
    )
    draw.add_to(m)
    LayerControl().add_to(m)
    return m



def dataframe_for_display(gdf, max_rows=200):
    cols = [c for c in gdf.columns if c != "geometry"] + ["geometry"]
    preview = gdf[cols].copy()
    preview["geometry"] = preview.geometry.astype(str)
    return preview.head(max_rows)



def get_drawn_geometry(map_output):
    if not map_output:
        return None

    # streamlit-folium typically returns either:
    # - last_active_drawing
    # - all_drawings
    last_active = map_output.get("last_active_drawing")
    all_drawings = map_output.get("all_drawings")

    candidate = None
    if last_active and isinstance(last_active, dict) and "geometry" in last_active:
        candidate = last_active
    elif all_drawings and isinstance(all_drawings, list) and len(all_drawings) > 0:
        # Use the most recent drawing
        last = all_drawings[-1]
        if isinstance(last, dict) and "geometry" in last:
            candidate = last

    if candidate is None:
        return None

    return shape(candidate["geometry"])



def save_gdf_as_shapefile_zip(gdf, output_name="edited_shapefile"):
    out_dir = tempfile.mkdtemp(prefix="shp_out_")
    shp_path = os.path.join(out_dir, f"{output_name}.shp")
    gdf.to_file(shp_path)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in Path(out_dir).glob(f"{output_name}.*"):
            zf.write(f, arcname=f.name)
    zip_buffer.seek(0)
    return zip_buffer


# =========================
# Session state
# =========================
if "gdf_original" not in st.session_state:
    st.session_state.gdf_original = None
if "gdf_current" not in st.session_state:
    st.session_state.gdf_current = None
if "gdf_wgs84" not in st.session_state:
    st.session_state.gdf_wgs84 = None
if "source_crs" not in st.session_state:
    st.session_state.source_crs = None
if "source_name" not in st.session_state:
    st.session_state.source_name = None
if "selected_index" not in st.session_state:
    st.session_state.selected_index = None
if "id_field" not in st.session_state:
    st.session_state.id_field = None


# =========================
# UI
# =========================
st.title("🗺️ Shapefile Geometry Editor")
st.caption(
    "Upload a zipped shapefile, select a feature, draw a replacement geometry on the map, and download the updated shapefile."
)

with st.expander("Instructions", expanded=True):
    st.markdown(
        """
**How this app works**
1. Upload a **ZIP** that contains your shapefile parts (`.shp`, `.shx`, `.dbf`, and ideally `.prj`).
2. Choose a feature to edit.
3. The selected feature is highlighted in **red**.
4. Use the drawing tools on the map to **draw the replacement geometry**.
5. Click **Apply drawn geometry to selected feature**.
6. Download the updated shapefile as a ZIP.

**Notes**
- This app uses a **redraw-and-replace** workflow rather than true vertex dragging.
- If your source data is not in WGS84, the app reprojects for map display and then transforms edits back to the original CRS before saving.
- If your shapefile has no CRS (`.prj` missing), the app will stop and ask you to fix it first.
        """
    )

uploaded = st.file_uploader("Upload a zipped shapefile", type=["zip"])

if uploaded is not None:
    try:
        file_bytes = uploaded.getvalue()
        gdf, shp_path, temp_dir = load_shapefile_from_zip(file_bytes, uploaded.name)

        if gdf.empty:
            st.error("The shapefile contains no features.")
            st.stop()

        if gdf.crs is None:
            st.error("This shapefile does not have a CRS. Please include a valid .prj file or assign a CRS before uploading.")
            st.stop()

        gdf = gdf.reset_index(drop=True).copy()
        gdf["_fid"] = gdf.index.astype(str)

        st.session_state.gdf_original = gdf.copy()
        if st.session_state.gdf_current is None or st.session_state.source_name != uploaded.name:
            st.session_state.gdf_current = gdf.copy()
            st.session_state.source_crs = gdf.crs
            st.session_state.source_name = uploaded.name
            st.session_state.id_field = "_fid"
            st.session_state.selected_index = 0

        current_gdf = st.session_state.gdf_current.copy()
        gdf_wgs84 = ensure_wgs84(current_gdf)
        st.session_state.gdf_wgs84 = gdf_wgs84

    except Exception as e:
        st.error(f"Failed to load shapefile: {e}")
        st.stop()

    left, right = st.columns([1, 2])

    with left:
        st.subheader("Data")
        st.write(f"**Features:** {len(st.session_state.gdf_current):,}")
        st.write(f"**Source CRS:** `{st.session_state.source_crs}`")
        st.write(f"**Geometry type(s):** {', '.join(sorted(set(st.session_state.gdf_current.geometry.geom_type.astype(str))))}")

        possible_id_fields = [c for c in st.session_state.gdf_current.columns if c != "geometry"]
        default_id_idx = possible_id_fields.index(st.session_state.id_field) if st.session_state.id_field in possible_id_fields else 0
        chosen_id = st.selectbox("Label field for feature selection", possible_id_fields, index=default_id_idx)
        st.session_state.id_field = chosen_id

        labels = (
            st.session_state.gdf_current[chosen_id].astype(str)
            + " | _fid="
            + st.session_state.gdf_current["_fid"].astype(str)
        )
        idx_map = dict(zip(labels.tolist(), st.session_state.gdf_current.index.tolist()))

        current_label = labels.iloc[st.session_state.selected_index] if st.session_state.selected_index in labels.index else labels.iloc[0]
        chosen_label = st.selectbox("Select feature to edit", labels.tolist(), index=labels.tolist().index(current_label))
        st.session_state.selected_index = idx_map[chosen_label]

        selected_row = st.session_state.gdf_current.loc[st.session_state.selected_index]
        st.markdown("**Selected feature attributes**")
        attrs = selected_row.drop(labels=["geometry"]).to_dict()
        st.json(attrs)

        with st.expander("Preview attribute table"):
            st.dataframe(dataframe_for_display(st.session_state.gdf_current), use_container_width=True, hide_index=True)

        if st.button("Reset edits", type="secondary"):
            st.session_state.gdf_current = st.session_state.gdf_original.copy()
            st.success("Edits reset to original uploaded shapefile.")
            st.rerun()

    with right:
        st.subheader("Map editor")
        st.info("The selected feature is shown in red. Draw a replacement geometry using the tools in the upper-right corner of the map.")

        map_obj = make_map(
            st.session_state.gdf_wgs84,
            selected_idx=st.session_state.selected_index,
            id_field=st.session_state.id_field,
        )

        map_output = st_folium(map_obj, width=None, height=650, returned_objects=["last_active_drawing", "all_drawings"])
        drawn_geom_wgs84 = get_drawn_geometry(map_output)

        if drawn_geom_wgs84 is not None:
            st.success(f"Detected drawn geometry: {drawn_geom_wgs84.geom_type}")
            st.code(drawn_geom_wgs84.wkt[:1200] + ("..." if len(drawn_geom_wgs84.wkt) > 1200 else ""), language="text")
        else:
            st.warning("No drawn geometry detected yet. Draw a point, line, or polygon to replace the selected geometry.")

        col_apply, col_download = st.columns([1, 1])

        with col_apply:
            if st.button("Apply drawn geometry to selected feature", type="primary", use_container_width=True):
                if drawn_geom_wgs84 is None:
                    st.error("Please draw a replacement geometry on the map first.")
                else:
                    try:
                        # Convert the drawn geometry from WGS84 back to source CRS
                        drawn_series = gpd.GeoSeries([drawn_geom_wgs84], crs="EPSG:4326").to_crs(st.session_state.source_crs)
                        new_geom = drawn_series.iloc[0]

                        updated = st.session_state.gdf_current.copy()
                        updated.at[st.session_state.selected_index, "geometry"] = new_geom
                        st.session_state.gdf_current = updated
                        st.session_state.gdf_wgs84 = ensure_wgs84(updated)
                        st.success("Geometry updated successfully.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not apply the geometry: {e}")

        with col_download:
            try:
                zip_bytes = save_gdf_as_shapefile_zip(st.session_state.gdf_current, output_name="edited_shapefile")
                st.download_button(
                    label="Download updated shapefile ZIP",
                    data=zip_bytes,
                    file_name="edited_shapefile.zip",
                    mime="application/zip",
                    use_container_width=True,
                )
            except Exception as e:
                st.error(f"Could not prepare download: {e}")

    st.divider()
    st.subheader("Current geometry summary")
    geom_summary = (
        st.session_state.gdf_current.geometry.geom_type.value_counts(dropna=False)
        .rename_axis("Geometry Type")
        .reset_index(name="Count")
    )
    st.dataframe(geom_summary, use_container_width=True, hide_index=True)

else:
    st.info("Upload a ZIP file containing a shapefile to begin.")
