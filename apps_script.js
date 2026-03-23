/**
 * HUJI Paper Monitor — Google Apps Script
 *
 * HOW TO INSTALL (one-time setup):
 * 1. Open your Google Sheet
 * 2. Click Extensions > Apps Script
 * 3. Delete any existing code and paste this entire file
 * 4. Click Save (floppy disk icon)
 * 5. Click Deploy > New deployment
 *    - Type: Web app
 *    - Execute as: Me
 *    - Who has access: Anyone
 * 6. Click Deploy — authorize when prompted
 * 7. Copy the Web App URL
 * 8. Add it as GitHub secret: APPS_SCRIPT_URL = <paste URL here>
 *
 * To redeploy after changes: Deploy > Manage deployments > Edit > New version > Deploy
 */

const SHEET_NAME = 'Sheet1';

/**
 * Receives POST requests from the Python pipeline.
 * Payload: { action: "replace_all", rows: [[col1, col2, ...], ...] }
 * The first row in `rows` is always the header.
 */
function doPost(e) {
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = ss.getSheetByName(SHEET_NAME) || ss.getActiveSheet();
    const payload = JSON.parse(e.postData.contents);

    if (payload.action === 'replace_all') {
      sheet.clearContents();
      const rows = payload.rows;
      if (rows && rows.length > 0) {
        sheet.getRange(1, 1, rows.length, rows[0].length).setValues(rows);
      }
      // Auto-resize columns for readability
      sheet.autoResizeColumns(1, rows[0].length);
    }

    return ContentService
      .createTextOutput(JSON.stringify({ status: 'ok', rows: payload.rows ? payload.rows.length : 0 }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ status: 'error', message: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

/** Simple health-check endpoint — visit the URL in a browser to confirm it works. */
function doGet(e) {
  return ContentService
    .createTextOutput('HUJI Paper Monitor Apps Script is running ✓')
    .setMimeType(ContentService.MimeType.TEXT);
}
