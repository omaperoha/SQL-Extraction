# How to Run the SQL Column Extractor

This guide is written for someone who has never used Python before on Windows.
Follow every step in order.

---

## Step 1 — Install Python

1. Open your web browser and go to: **https://www.python.org/downloads/**
2. Click the big yellow button that says **"Download Python 3.x.x"**
3. Once the file downloads, run it (double-click the `.exe` file)
4. **IMPORTANT:** On the first screen of the installer, check the box that says:
   > ☑ Add Python to PATH

   If you skip this, Python will not work from the command line.
5. Click **"Install Now"** and wait for it to finish
6. Click **Close** when done

### Verify the installation

1. Press `Windows key + R`, type `cmd`, press Enter — this opens **Command Prompt**
2. Type the following and press Enter:
   ```
   python --version
   ```
3. You should see something like:
   ```
   Python 3.12.3
   ```
   If you see an error instead, re-run the Python installer and make sure to check **"Add Python to PATH"**.

---

## Step 2 — Download the Project

If you received the project as a ZIP file:
1. Unzip it to a folder, for example: `C:\Users\YourName\Documents\SQL-Extraction`

If you have Git installed:
```
git clone https://github.com/omaperoha/SQL-Extraction.git
```

---

## Step 3 — Install the Required Libraries

The script uses three Python libraries. You install them all with one command.

1. Open **Command Prompt** (`Windows key + R` → type `cmd` → Enter)
2. Navigate to the project folder. Replace the path below with your actual path:
   ```
   cd C:\Users\YourName\Documents\SQL-Extraction
   ```
   > **Tip:** You can type `cd ` (with a space) and then drag-and-drop the folder from
   > Windows Explorer into the Command Prompt window — it fills in the path automatically.
3. Run:
   ```
   pip install -r requirements.txt
   ```
4. Wait for the installation to finish. You will see lines scrolling by — that is normal.
   When it says `Successfully installed ...` you are ready.

---

## Step 4 — Run the Script

### Basic usage

```
python sql_extractor.py "C:\path\to\your\sql_folder"
```

Replace `C:\path\to\your\sql_folder` with the actual path to the folder that contains
your `.sql` files. Put the path in quotes if it contains spaces.

### Example — using the built-in test files

If you are inside the `SQL-Extraction` folder:
```
python sql_extractor.py tests\sql_samples
```

### Alternative syntax (named flag)

```
python sql_extractor.py --folder "C:\path\to\your\sql_folder"
```

---

## Step 5 — View the Output

After running, the script creates an Excel file **inside the same folder as your .sql files**.

The file is named:
```
<folder_name>_columns_YYYYMMDD_HHMMSS.xlsx
```

Example:
```
sql_samples_columns_20260313_142500.xlsx
```

Open it in **Microsoft Excel** or any spreadsheet application.

### Output columns

| Column | Description |
|--------|-------------|
| `file` | Name of the `.sql` file the reference was found in |
| `table_name` | Table alias or name used to qualify the column (e.g. `c` in `c.customer_id`). Empty if the column was not qualified. |
| `column_name` | The column identifier. `*` means `SELECT *`. |
| `clause` | Which part of the SQL the column appeared in: `SELECT`, `WHERE`, `JOIN ON`, `GROUP BY`, `HAVING`, `ORDER BY`, or `FROM/JOIN (table)` |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `'python' is not recognized as an internal or external command` | Python is not on your PATH. Re-run the Python installer and check **"Add Python to PATH"**. |
| `No module named 'sqlglot'` | Run `pip install -r requirements.txt` from the project folder. |
| `pip is not recognized` | Same as above — Python is not on PATH. Re-run installer. |
| `No .sql files found in ...` | Check that your folder path is correct and that the files end in `.sql`. |
| A file is skipped with a WARNING | That file has SQL syntax the parser could not understand. All other files are still processed. |
| `Permission denied` | Right-click on Command Prompt and choose **"Run as administrator"**, then re-run the command. |

---

## Notes

- The script only scans `.sql` files in the **top-level** of the specified folder.
  It does not look inside subfolders.
- Running the script multiple times creates a new `.xlsx` file each time
  (the timestamp in the filename keeps them separate).
- The script never modifies your `.sql` files — it only reads them.
