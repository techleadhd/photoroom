# PhotoRoom - Apple Photos to Lightroom converter

Bring the look of your Apple Photos edits into **Lightroom Classic**, while
keeping your originals editable. PhotoRoom compares your edited photos with
Lightroom renders and adjusts the Lightroom settings to get a close match.

It matches temperature, tint, exposure, contrast, highlights, shadows, whites,
blacks, crop, and rotation. Other settings stay unchanged. Matches are approximate;
review them before moving on from Apple Photos.

<img width="2336" height="1045" alt="photoroom2" src="https://github.com/user-attachments/assets/9321a1af-2b0e-434a-a950-a8fa62930697" />

## 1. Export from Apple Photos

Create two folders, `orig` and `edit`. Start with a few photos to try the workflow.

Select the same photos for both exports:

- **Originals:** choose **File > Export > Export Unmodified Original** and save
  to `orig`.
- **Edited versions:** choose **File > Export > Export Photos**, select **TIFF**,
  enable **16-bit**, choose **Full Size** and **Display P3**, and save to `edit`.
  Keep the color profile embedded. sRGB also works if P3 is unavailable.

**Export IPTC as XMP** is optional: enable it if you want to preserve metadata
and keywords added in Apple Photos. Apple creates extra `.xmp` files in `orig`;
PhotoRoom leaves them alone and does not use them for matching or import their
metadata into Lightroom. Leave it off if you only need to match the photo edits
and want no sidecars in `orig`.

Use the original filenames for both exports. Names must match apart from the
extension; if you use subfolders, their paths must match too:

```text
orig/IMG_001.ARW
edit/IMG_001.tiff
```

See [Apple’s export instructions](https://support.apple.com/guide/photos/pht6e157c5f/mac)
for help. PhotoRoom matches photo edits; it does not migrate albums or videos.

## 2. Set up Lightroom

Requires **macOS** and **Lightroom Classic 12+**.

**First-time setup:** open Terminal (**Command–Space**, type **Terminal**, press
Return). If you don’t have Homebrew, install it using the instructions at
[brew.sh](https://brew.sh), including the installer’s **Next steps** commands.
Then run:

```bash
brew install python little-cms2
```

This installs Python and the color-processing library. Skip this step if you
already have Python 3.10+ and LittleCMS. PhotoRoom handles its remaining
dependencies automatically; no environment activation is needed.

**In Lightroom:**

1. Import the photos in `orig` into Lightroom using **Add**, so they stay in
   that folder.
2. Open **File > Plug-in Manager**, click **Add**, and select
   **`PhotoRoom.lrplugin`** inside the PhotoRoom project folder.
3. Make sure the plug-in is **Enabled**. It connects automatically when you run
   PhotoRoom—there is no bridge command to start.

Keep Lightroom open and avoid editing photos while matching is running.
If updating an existing installation, reload the plug-in in Plug-in Manager
to activate the automatic listener (version 0.3.0).

## 3. Run PhotoRoom

In Terminal, go to your PhotoRoom folder. Replace the example paths below with
your own folder paths:

```bash
cd "/path/to/PhotoRoom"
```

**First, check one photo without making changes.** This previews the filename
pairing; it does not evaluate the match or require Lightroom.

```bash
python3 photoroom.py --orig "/path/to/orig" --edit "/path/to/edit" --dry-run --limit 1
```

**Next, match one photo.** With Lightroom open and the plug-in enabled, run:

```bash
python3 photoroom.py --orig "/path/to/orig" --edit "/path/to/edit" --limit 1
```

Review that photo in Lightroom and its comparison preview before continuing.

**Then, match the full batch:**

```bash
python3 photoroom.py --orig "/path/to/orig" --edit "/path/to/edit"
```

PhotoRoom skips completed photos, including the one you just tested, then shows
matching progress and estimated time remaining.

## 4. Review the results

**Your matched edits are already in Lightroom’s catalog.** You do not need to
use “Read Metadata from File.” PhotoRoom does not write to your originals or
require Lightroom’s “Include Develop settings in metadata” checkbox.

Beside each edited TIFF, PhotoRoom saves:

- **`.match-preview.jpg`** — a comparison of the starting image, Apple Photos
  edit, and best match.
- **`.xmp`** — a record of the matched controls that also marks the photo as
  completed. This is not a full backup of your Lightroom edits.

The final summary lists completed photos and repeats any warnings or failures:

- **SAVED:** matching completed.
- **WARNING:** the closest result was saved, but needs review.
- **FAILED:** matching could not complete; check the accompanying message.

Lightroom can also write metadata itself. If you want originals to remain
untouched, turn off **Catalog Settings > Metadata > Automatically write changes
into XMP**.

## Run again

Run the same command whenever you’re ready. Completed photos are skipped.
**To redo one photo, delete its `.xmp` from `edit`**, then run again.
Its preview will be replaced automatically.

Add an option to the command when needed:

| Option | What it does |
| --- | --- |
| `--limit 1` | Try one photo that still needs matching |
| `--overwrite` | Redo all photos, including completed ones |
| `--virtual-copies` | Apply matches to virtual copies in Lightroom |
| `--prefer raw` | Use the RAW when a RAW and JPEG share the same name |

Matching stops automatically when Python finishes. If Python crashes, the plug-in
cleans up after about 30 seconds, once any current Lightroom operation finishes.
Completed matches are kept; unfinished edits on originals are restored.

## Wrapping up
**After migration, disable PhotoRoom in File > Plug-in Manager.** While enabled,
it checks for a new run every two seconds without showing an idle progress bar.
You can also stop a Python run with **Ctrl+C** in Terminal.

For manual control, use **Library > Plug-in Extras**:

- **PhotoRoom: show listener status** tells you whether it is ready, matching,
  starting, cleaning up, or stopping/stopped.
- **PhotoRoom: start listener** starts it if needed and shows its current status.
- **PhotoRoom: stop listener** requests a stop and confirms it. It may need to
  finish the current operation first; completed matches are kept.

After manually stopping the listener, choose **start listener** before the next
Python run, or disable and re-enable the plug-in. These commands are optional;
normal runs start and finish automatically.

If you move the PhotoRoom folder, stop Python and disable the plug-in first,
then add the plug-in again from its new location.

Free for personal, noncommercial use only. Business or professional use requires
written permission. See [LICENSE](LICENSE).
