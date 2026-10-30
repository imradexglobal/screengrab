"""Region management for Screen Monitor.

Handles creation, editing, deletion, and validation of screen regions.
Each region has a stable region_id for correct duplicate suppression.
"""

import logging
import time
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from PySide6.QtCore import QObject, Signal, Slot, Qt, QRect
from PySide6.QtGui import QColor, QPen, QBrush

from src.database.init_db import get_connection, initialise_database, DB_PATH
from src.capture.screen_capture import primary_monitor, all_monitors

logger = logging.getLogger(__name__)


class Region:
    """Represents a screen monitoring region with stable identity."""
    
    def __init__(self, region_id: str, name: str, monitor_id: str,
                 x: int, y: int, width: int, height: int, enabled: bool = True):
        self.region_id = region_id
        self.name = name
        self.monitor_id = monitor_id
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.enabled = enabled
        self.created_at: Optional[str] = None
        self.updated_at: Optional[str] = None

    # ------------------------------------------------------------------
    # Coordinate safety
    # ------------------------------------------------------------------

    @property
    def is_valid(self) -> bool:
        """True when the stored bounding box is geometrically usable.

        Requires non-negative coordinates, strictly positive dimensions, and a
        rectangle that lies inside the *virtual desktop* (all monitors), so a
        region on a secondary display is accepted while an off-screen or
        zero-sized selection is rejected.  When monitor geometry cannot be
        determined the check falls back to the basic sanity rules only.
        """
        if self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            return False
        try:
            monitors = all_monitors()
            if monitors:
                virtual = monitors[0]  # MSS index 0 spans every display
                vx = virtual.get("left", 0)
                vy = virtual.get("top", 0)
                vw = virtual.get("width", 0)
                vh = virtual.get("height", 0)
                if vw and vh:
                    if self.x + self.width > vx + vw or self.y + self.height > vy + vh:
                        return False
        except Exception:
            # Capture metadata unavailable - accept only clearly valid rectangles.
            pass
        return True
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert region to dictionary for database/storage."""
        return {
            "region_id": self.region_id,
            "name": self.name,
            "monitor_id": self.monitor_id,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "enabled": 1 if self.enabled else 0,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Region":
        """Create Region from dictionary."""
        region = cls(
            region_id=data["region_id"],
            name=data["name"],
            monitor_id=data["monitor_id"],
            x=data["x"],
            y=data["y"],
            width=data["width"],
            height=data["height"],
            enabled=bool(data.get("enabled", True)),
        )
        region.created_at = data.get("created_at")
        region.updated_at = data.get("updated_at")
        return region


class RegionManager(QObject):
    """Manages screen monitoring regions with persistent storage."""
    
    # Qt signals for GUI communication
    region_added = Signal(dict)       # emitted when region is added
    region_removed = Signal(str)      # emitted with region_id
    region_updated = Signal(dict)     # emitted with region data
    regions_loaded = Signal(list)     # emitted with list of region dicts
    
    def __init__(self, parent: Optional[QObject] = None, db_path=None):
        super().__init__(parent)
        self.db_path = db_path or DB_PATH
        self._regions: Dict[str, Region] = {}
        self._conn = None
        self._initialized = False
        
    def initialise(self) -> bool:
        """Initialise the region manager with database connection."""
        try:
            self._conn = get_connection(self.db_path)
            # Ensure regions table exists (initialise_database should have created it)
            self._load_regions_from_db()
            self._initialized = True
            logger.info(f"RegionManager initialised with {len(self._regions)} regions")
            self.regions_loaded.emit(list(self._regions.values()))
            return True
        except Exception as e:
            logger.error(f"Failed to initialise RegionManager: {e}")
            return False
    
    def _load_regions_from_db(self) -> None:
        """Load all regions from the database."""
        if self._conn is None:
            return
        try:
            cursor = self._conn.execute(
                "SELECT region_id, name, monitor_id, x, y, width, height, enabled, created_at, updated_at FROM regions"
            )
            self._regions = {}
            for row in cursor:
                (region_id, name, monitor_id, x, y, width, height, 
                 enabled, created_at, updated_at) = row
                region = Region(
                    region_id=region_id,
                    name=name,
                    monitor_id=monitor_id,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    enabled=bool(enabled),
                )
                region.created_at = created_at
                region.updated_at = updated_at
                self._regions[region_id] = region
            logger.info(f"Loaded {len(self._regions)} regions from database")
        except Exception as e:
            logger.error(f"Error loading regions from DB: {e}")
    
    def get_all_regions(self) -> List[Region]:
        """Get all regions (enabled and disabled)."""
        return list(self._regions.values())
    
    def get_enabled_regions(self) -> List[Region]:
        """Get only enabled regions."""
        return [r for r in self._regions.values() if r.enabled]
    
    def get_region(self, region_id: str) -> Optional[Region]:
        """Get a region by its stable ID."""
        return self._regions.get(region_id)
    
    def add_region(self, name: str, monitor_id: str, x: int, y: int,
                   width: int, height: int, enabled: bool = True) -> Optional[Region]:
        """Add a region, assigning one of the two fixed slots.

        The application exposes exactly two region slots (spec section 5):

        * ``region-A`` - the active reader.  It is created by the first
          successful call, whatever ``name`` the caller supplies.
        * ``region-B`` - the secondary slot.  It may be enabled and is
          captured by the pipeline, but its rows are written to the store-only
          ``region_b_readings`` table: they never feed learning, predictions,
          statistics or clicks.

        A third region is refused.  When Region A already exists the caller is
        expected to use :meth:`edit_region` to recalibrate it.
        """
        if self._conn is None:
            return None

        # A name must be unique across the two slots.
        cursor = self._conn.execute("SELECT COUNT(*) AS n FROM regions WHERE name = ?", (name,))
        if cursor.fetchone()["n"] > 0:
            logger.warning("A region named '%s' already exists; refusing duplicate.", name)
            return None

        # Choose the slot.
        if self.get_region("region-A") is None and not self._region_exists_in_db("region-A"):
            region_id = "region-A"
            enabled = True
        elif self.get_region("region-B") is None and not self._region_exists_in_db("region-B"):
            region_id = "region-B"
            # Store-only slot: the caller decides whether it starts capturing.
        else:
            logger.warning(
                "Both region slots are already defined; refusing to add '%s'. "
                "Recalibrate Region A instead.", name,
            )
            return None

        # Geometry validation before anything is persisted.
        if not all(isinstance(v, int) for v in (x, y, width, height)):
            logger.error("Coordinates must be integers.")
            return None
        if width <= 0 or height <= 0:
            logger.error("Region dimensions must be positive.")
            return None

        region = Region(
            region_id=region_id,
            name=name,
            monitor_id=monitor_id,
            x=x, y=y, width=width, height=height,
            enabled=enabled,
        )
        if not region.is_valid:
            logger.error("Region bounding box is outside the virtual desktop.")
            return None

        now = datetime.now(timezone.utc).isoformat()
        region.created_at = now
        region.updated_at = now

        try:
            self._conn.execute(
                """INSERT INTO regions (region_id, name, monitor_id, x, y, width, height, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (region_id, name, monitor_id, x, y, width, height,
                 int(enabled), now, now),
            )
            self._conn.commit()
        except Exception as e:
            logger.error(f"Error saving region to database: {e}")
            return None

        self._regions[region_id] = region
        self.region_added.emit(region.to_dict())
        logger.info("Added region %s: %s (enabled=%s)", region_id, name, enabled)
        return region
    
    def region_id_exists(self, region_id: str) -> bool:
        """Check if a region_id already exists."""
        # Check both in memory and database
        if region_id in self._regions:
            return True
        if self._conn:
            try:
                cursor = self._conn.execute(
                    "SELECT 1 FROM regions WHERE region_id = ?", (region_id,)
                )
                return cursor.fetchone() is not None
            except Exception:
                pass
        return False
    
    def rename_region(self, region_id: str, new_name: str) -> bool:
        """Rename a region."""
        if region_id not in self._regions:
            logger.warning(f"Region {region_id} does not exist")
            return False
        
        region = self._regions[region_id]
        region.name = new_name
        region.updated_at = datetime.now(timezone.utc).isoformat()
        
        try:
            self._conn.execute(
                "UPDATE regions SET name = ?, updated_at = ? WHERE region_id = ?",
                (new_name, region.updated_at, region_id),
            )
            self._conn.commit()
        except Exception as e:
            logger.error(f"Error renaming region: {e}")
            return False
        
        # Emit signal
        self.region_updated.emit(region.to_dict())
        logger.info(f"Renamed region {region_id} to {new_name}")
        return True
    
    def edit_region(self, region_id: str, x: int, y: int, width: int, height: int, 
                    enabled: bool) -> bool:
        """Edit region coordinates and enabled status."""
        if region_id not in self._regions:
            logger.warning(f"Region {region_id} does not exist")
            return False
        
        region = self._regions[region_id]
        region.x = x
        region.y = y
        region.width = width
        region.height = height
        region.enabled = enabled
        region.updated_at = datetime.now(timezone.utc).isoformat()
        
        try:
            self._conn.execute(
                "UPDATE regions SET x = ?, y = ?, width = ?, height = ?, enabled = ?, updated_at = ? WHERE region_id = ?",
                (x, y, width, height, int(enabled), region.updated_at, region_id),
            )
            self._conn.commit()
        except Exception as e:
            logger.error(f"Error editing region: {e}")
            return False
        
        # Emit signal
        self.region_updated.emit(region.to_dict())
        logger.info(f"Edited region {region_id}")
        return True
    
    def delete_region(self, region_id: str) -> bool:
        """Delete a region (preserves historical detections)."""
        if region_id not in self._regions and not self._region_exists_in_db(region_id):
            logger.warning(f"Region {region_id} does not exist")
            return False
        
        # Remove from memory (but keep DB row, detections preserved)
        if region_id in self._regions:
            region = self._regions[region_id]
            # We soft-delete by setting enabled=0 rather than removing the row
            # This preserves historical detections
            try:
                self._conn.execute(
                    "UPDATE regions SET enabled = 0, updated_at = ? WHERE region_id = ?",
                    (datetime.now(timezone.utc).isoformat(), region_id),
                )
                self._conn.commit()
            except Exception as e:
                logger.error(f"Error soft-deleting region: {e}")
                return False
            
            del self._regions[region_id]
            self.region_removed.emit(region_id)
            logger.info(f"Soft-deleted region {region_id} (detections preserved)")
            return True
        
        # If not in memory but in DB, just soft-delete
        if self._region_exists_in_db(region_id):
            try:
                self._conn.execute(
                    "UPDATE regions SET enabled = 0, updated_at = ? WHERE region_id = ?",
                    (datetime.now(timezone.utc).isoformat(), region_id),
                )
                self._conn.commit()
                self._load_regions_from_db()  # Reload from DB
                self.region_removed.emit(region_id)
                logger.info(f"Soft-deleted region {region_id} from DB (detections preserved)")
                return True
            except Exception as e:
                logger.error(f"Error soft-deleting region from DB: {e}")
                return False
        
        return False
    
    def _region_exists_in_db(self, region_id: str) -> bool:
        """Check if region exists in database (even if disabled)."""
        if self._conn is None:
            return False
        try:
            cursor = self._conn.execute("SELECT 1 FROM regions WHERE region_id = ?", (region_id,))
            return cursor.fetchone() is not None
        except Exception:
            return False
    
    def revalidate_regions(self) -> int:
        """Revalidate region coordinates when monitor layout changes.

        For now returns the count of enabled regions reloaded from DB
        (monitor identity mapping is approximated by current MSS monitors).
        """
        # Reload from DB; actual DPI/resolution adjustment would require
        # matching stored monitor_id against current MSS geometry.
        self._load_regions_from_db()
        return len(self._regions)

    # ------------------------------------------------------------------
    # Two-region model enforcement
    # ------------------------------------------------------------------

    def set_region_a_active(self) -> None:
        """Ensure exactly Region A exists as the single active region."""
        # Existing regions are preserved; this only affects the *role*
        # assignment used downstream by the engine. DB rows stay intact.
        for rid, r in list(self._regions.items()):
            if r.region_id == "region-A":
                continue  # already active
            if rid == "region-B":
                r.enabled = False
                r.updated_at = datetime.now(timezone.utc).isoformat()
                self._conn.execute(
                    "UPDATE regions SET enabled = 0, updated_at = ? WHERE region_id = ?",
                    (r.updated_at, rid),
                )
                self._conn.commit()

    def set_region_b_reserved(self) -> None:
        """Ensure Region B is present and marked reserved (inactive).

        Region B may exist (preserving history) but must never feed readings
        into learning, predictions, or clicks. It is visually shown inactive.
        """
        try:
            # Make sure region-A exists and is active
            existing = [r for r in self._regions.values() if r.region_id == "region-A"]
            if not existing:
                return  # nothing to reorganize yet
            self.set_region_a_active()

            # Region B either exists already (ensure disabled) or we create it
            exists = self.get_region("region-B")
            if exists is not None:
                if not exists.enabled:
                    # Re-enable Region A; keep Region B reserved/disabled
                    for r in self._regions.values():
                        if r.region_id == "region-A":
                            r.enabled = True
                            r.updated_at = datetime.now(timezone.utc).isoformat()
                            self._conn.execute(
                                "UPDATE regions SET enabled = 1, updated_at = ? WHERE region_id = ?",
                                (r.updated_at, r.region_id),
                            )
                            break
                return

            # Create Region B as a reserved slot using default monitor coords
            primary = primary_monitor()
            x = int(primary.get("left", 0) or 0)
            y = int(primary.get("top", 0) or 0)
            w = int(primary.get("width", 800) or 800)
            h = int(primary.get("height", 600) or 600)
            name = "Region B (Reserved)"
            rid = f"region-B"
            # Insert directly bypassing name collision check to allow idempotent creation
            now = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "INSERT OR IGNORE INTO regions (region_id, name, monitor_id, x, y, width, height, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, name, "secondary", x, y, w, h, 0, now, now),
            )
            self._conn.commit()
            reg = Region(
                region_id=rid,
                name=name,
                monitor_id="secondary",
                x=x, y=y, width=w, height=h,
                enabled=False,
            )
            self._regions[rid] = reg
        except Exception as e:
            logger.warning(f"Region B reservation skipped (monitor geometry unavailable): {e}")


# -------------------------------------------------------------------------
# Factory function
# -------------------------------------------------------------------------

def create_region_manager(db_path=None) -> RegionManager:
    """Factory function to create a configured RegionManager instance."""
    manager = RegionManager(db_path=db_path)
    return manager