"""Card renderer — layout robustness at real frame sizes."""
from PIL import Image, ImageDraw

from newsroom.models.shot import Shot, VisualPlan
from newsroom.visuals.renderer import PALETTE, CardRenderer


def _plan(w=1080, h=1920):
    return VisualPlan(event_id="e", revision=1, width=w, height=h, fps=30)


def test_metric_grid_wraps_long_values_inside_cell():
    """A long metric value must stay inside its cell — before the wrap
    fix it overflowed the cell edge and clipped silently."""
    r = CardRenderer()
    img = Image.new("RGB", (1080, 1920), PALETTE["bg"])
    draw = ImageDraw.Draw(img)
    shot = Shot(type="metric_grid", duration_s=5, params={
        "title": "Event Data",
        "metrics": [
            {"label": "EVENT",
             "value": "FEMA Major Disaster Declaration: "
                      "Hurricane Isaias — FL"},
            {"label": "TIME UTC", "value": "00:00 UTC, 06 Oct"},
            {"label": "STATUS", "value": "declared"},
        ]})
    r._shot_metric_grid(img, draw, shot, _plan(), {})

    # The 40px gutter between the two columns should stay pure
    # background — any bright pixel there is text bleeding out.
    cell_w = (1080 - 80 - 40) // 2
    gap = img.crop((40 + cell_w + 2, 240, 40 + cell_w + 38, 500))
    bright = sum(1 for px in gap.getdata() if sum(px) > 600)
    assert bright == 0

    # Sanity: the wrapped value did render (text pixels inside cell 1).
    cell = img.crop((60, 300, 40 + cell_w - 10, 440))
    assert sum(1 for px in cell.getdata() if sum(px) > 600) > 50
