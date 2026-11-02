# Plan: Pattern System Upgrade + Region B Debug & Upgrade

## Goal
1. Upgrade the pattern system so the user has full control to add/delete patterns at will, with patterns defined by the user (no automatic generation of thousands of patterns).
2. Enable patterns to be based on:
   - **Range patterns**: Sequences of group identifiers (g1-g9), e.g., "g3,g4,g3"
   - **Movement patterns**: Sequences of raw number directions (UP/DOWN/FLAT), e.g., "UP,DOWN,UP,DOWN" where UP means current value is higher than previous at two decimal places
3. Upgrade Region B to feel and work exactly like Region A (while maintaining its store-only nature), with matching UI and behavior.

## Current State Analysis

### Pattern System
- `src/learning/manual_brain.py` already supports two kinds of patterns:
  - `KIND_RANGE`: Sequence of category identifiers (groups), predicts next category
  - `KIND_MOVEMENT`: Sequence of movement tokens (UP/DOWN/FLAT), predicts next movement
- Patterns are stored in `pattern_definitions` table via `repository.py`
- `ManualBrain` class uses `LearningEngine` for incremental learning
- UI pattern management is in `main_window.py` via `list_patterns()`, `add_pattern()`, `delete_pattern()`, `set_pattern_enabled()`, `update_pattern_label()`
- Currently, the app can still auto-learn patterns from Region A readings via the incremental learning path

### Region B
- Region B is defined in `src/monitor/region_manager.py` with two fixed slots: region-A and region-B
- Region B stores readings in `region_b_readings` table (store-only)
- Region B ignores range classification (`require_range = region_id != REGION_B_ID` in engine.py:615)
- Region B UI in `RegionsWidget` shows "Inactive — not configured" when disabled
- The border overlay and HUD only work properly for Region A - Region B border/HUD logic needs matching

## Changes Required

### 1. Pattern System Enhancements
- **No changes needed to core pattern engine** - it already supports user-defined range and movement patterns
- Ensure UI pattern management fully supports add/delete/enable/disable
- Add ability to define movement patterns (UP/DOWN/FLAT sequences) via the UI
- Ensure pattern tokens are validated: range tokens must be valid group_ids (g1-g9), movement tokens must be UP/DOWN/FLAT

### 2. Region B Upgrade to Match Region A
**UI Changes** (`src/ui/main_window.py` - `RegionsWidget`):
- Make Region B border overlay behave identically to Region A (green frame, capture exclusion)
- Add prediction HUD below Region B (currently only anchored below Region A)
- Refresh Region B stats to match Region A format
- Ensure "Activate/Select Region B" works identically to Region A

**Engine Changes** (`src/monitor/engine.py`):
- Region B should still be store-only (data never feeds learning/gate/clicks) - this is by design
- But the UI and monitoring experience should match Region A
- Ensure Region B border is shown, can be enabled/disabled, and has same visual feedback

**Database/Storage**:
- Region B already stores in separate table - no changes needed
- Ensure Region B readings are visible/accessible in UI

### 3. Movement Pattern Support
- Movement patterns (UP/DOWN/FLAT) already supported in `manual_brain.py` and `repository.py`
- Need to ensure UI can add movement patterns with tokens like "UP,DOWN,UP,DOWN"
- Validate movement tokens: only UP, DOWN, FLAT allowed

## Implementation Task List

1. **Pattern UI enhancements** in `RegionsWidget` or dedicated pattern management page
2. **Region B UI matching** - border overlay, HUD, stats display
3. **Ensure movement pattern tokens** are properly validated in UI and repo
4. **Test the complete flow**: add range pattern, add movement pattern, verify Region B works like Region A

## Risks
- Region B changes must maintain the store-only invariant (data never feeds learning/gate/clicks)
- Pattern token validation must be strict for movement patterns (only UP/DOWN/FLAT)
- UI changes must be consistent between Region A and Region B

## Validation
- Add a range pattern (e.g., "g3,g4,g3") and verify it appears in pattern list
- Add a movement pattern (e.g., "UP,DOWN,UP,DOWN") and verify it appears in pattern list  
- Activate Region B, capture readings, verify they store in region_b_readings table
- Verify Region B border appears and matches Region A styling
- Verify Region B readings do NOT affect automation gate or predictions