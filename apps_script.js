/**
 * Google Apps Script for Sheet 1 (Raw Orders)
 *
 * SETUP:
 * 1. Open Sheet 1 → Extensions → Apps Script
 * 2. Paste this entire file and Save
 * 3. Project Settings → Script Properties → add:
 *      API_BASE_URL      = https://YOUR-RENDER-URL  (no trailing slash)
 *      WEBHOOK_SECRET    = same value as SHEET_WEBHOOK_SECRET in FastAPI .env
 * 4. Triggers → Add Trigger:
 *      Function: checkNewOrders
 *      Event source: Time-driven
 *      Type: Minutes timer → Every 1 minute
 *    (Optional) also add an onEdit installable trigger calling checkNewOrders
 *
 * Sheet columns (row 1 = headers):
 *   A Nom | B Num | C Adress | D Prix | E Ville | F Model | G Status
 */

var STATUS_COL = 7; // Column G
var HEADER_ROW = 1;

function getConfig_() {
  var props = PropertiesService.getScriptProperties();
  var baseUrl = (props.getProperty("API_BASE_URL") || "").replace(/\/$/, "");
  var secret = props.getProperty("WEBHOOK_SECRET") || "";
  if (!baseUrl || !secret) {
    throw new Error(
      "Set Script Properties API_BASE_URL and WEBHOOK_SECRET before running."
    );
  }
  return { baseUrl: baseUrl, secret: secret };
}

/**
 * Find rows with Nom + Num filled and Status empty, mark Sending..., POST to FastAPI.
 */
function checkNewOrders() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
  var lastRow = sheet.getLastRow();
  if (lastRow <= HEADER_ROW) {
    return;
  }

  var config = getConfig_();
  var numRows = lastRow - HEADER_ROW;
  var data = sheet.getRange(HEADER_ROW + 1, 1, numRows, 7).getValues();

  for (var i = 0; i < data.length; i++) {
    var row = data[i];
    var sheetRow = HEADER_ROW + 1 + i; // 1-based sheet row index

    var nom = String(row[0] || "").trim();
    var num = String(row[1] || "").trim();
    var adress = String(row[2] || "").trim();
    var prix = row[3];
    var ville = String(row[4] || "").trim();
    var model = String(row[5] || "").trim();
    var status = String(row[6] || "").trim();

    if (!nom || !num) {
      continue;
    }
    if (status !== "") {
      continue;
    }

    // Claim the row so concurrent triggers do not double-send
    sheet.getRange(sheetRow, STATUS_COL).setValue("Sending...");
    SpreadsheetApp.flush();

    var payload = {
      row_index: sheetRow,
      nom: nom,
      num: num,
      adress: adress,
      prix: prix,
      ville: ville,
      model: model,
    };

    try {
      var response = UrlFetchApp.fetch(config.baseUrl + "/webhook/new-order", {
        method: "post",
        contentType: "application/json",
        headers: {
          "x-webhook-secret": config.secret,
        },
        payload: JSON.stringify(payload),
        muteHttpExceptions: true,
      });

      var code = response.getResponseCode();
      if (code < 200 || code >= 300) {
        Logger.log(
          "Row " +
            sheetRow +
            " failed HTTP " +
            code +
            ": " +
            response.getContentText()
        );
        // Reset so the next trigger can retry
        sheet.getRange(sheetRow, STATUS_COL).setValue("");
      } else {
        Logger.log("Row " + sheetRow + " sent OK");
      }
    } catch (err) {
      Logger.log("Row " + sheetRow + " error: " + err);
      sheet.getRange(sheetRow, STATUS_COL).setValue("");
    }
  }
}

/**
 * Optional: installable onEdit trigger can call this wrapper.
 * Simple onEdit (non-installable) cannot use UrlFetchApp.
 */
function onEditInstallable(e) {
  checkNewOrders();
}
