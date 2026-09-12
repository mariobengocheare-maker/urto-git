"""
Flask Blueprint for every CRM/Dialer/Transactions/Calendar route — the part
of URTO that has zero Playwright/Skip-Trace dependency and is shared
verbatim between the desktop app (app.py) and the hosted phone app
(hosted_app.py, see CLAUDE.md "Hosted CRM/Dialer"). Registering the same
blueprint in both means there is exactly one implementation of this API to
keep correct, not two copies that can drift apart.
"""

import json

from flask import Blueprint, abort, jsonify, request, send_file

import crm

crm_bp = Blueprint("crm_bp", __name__)


@crm_bp.route("/api/crm/clients", methods=["GET"])
def crm_list_clients():
    return jsonify(crm.list_clients())


@crm_bp.route("/api/crm/clients", methods=["POST"])
def crm_create_client():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        client = crm.create_client(
            name=name,
            phone=(data.get("phone") or "").strip(),
            contact_info=(data.get("contact_info") or "").strip(),
            address=(data.get("address") or "").strip(),
            frequency_key=data.get("frequency_key"),
            custom_amount=data.get("custom_amount"),
            custom_unit=data.get("custom_unit"),
            list_ids=data.get("list_ids"),
            start_date=(data.get("start_date") or "").strip() or None,
            email=(data.get("email") or "").strip() or None,
        )
    except crm.DuplicatePhoneError as e:
        return jsonify({"error": str(e)}), 409
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid custom follow-up amount"}), 400
    return jsonify(client)


@crm_bp.route("/api/crm/clients/<int:client_id>", methods=["GET"])
def crm_get_client(client_id):
    client = crm.get_client(client_id)
    if not client:
        abort(404)
    client["notes"] = crm.list_notes(client_id)
    client["list_ids"] = crm.get_client_list_ids(client_id)
    return jsonify(client)


@crm_bp.route("/api/crm/clients/<int:client_id>", methods=["PUT"])
def crm_update_client(client_id):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        client = crm.update_client(
            client_id,
            name=name,
            phone=(data.get("phone") or "").strip(),
            contact_info=(data.get("contact_info") or "").strip(),
            address=(data.get("address") or "").strip(),
            frequency_key=data.get("frequency_key"),
            custom_amount=data.get("custom_amount"),
            custom_unit=data.get("custom_unit"),
            list_ids=data.get("list_ids"),
            email=(data.get("email") or "").strip() or None,
        )
    except crm.DuplicatePhoneError as e:
        return jsonify({"error": str(e)}), 409
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid custom follow-up amount"}), 400
    if not client:
        abort(404)
    return jsonify(client)


@crm_bp.route("/api/crm/clients/<int:client_id>", methods=["DELETE"])
def crm_delete_client(client_id):
    crm.delete_client(client_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/duplicates", methods=["GET"])
def crm_find_duplicates():
    return jsonify(crm.find_duplicate_clients())


@crm_bp.route("/api/crm/duplicates/merge", methods=["POST"])
def crm_merge_duplicates():
    data = request.get_json(force=True)
    keep_id = data.get("keep_id")
    remove_ids = data.get("remove_ids") or []
    if not keep_id or not remove_ids:
        return jsonify({"error": "keep_id and remove_ids are required"}), 400
    result = crm.merge_clients(keep_id, remove_ids)
    if not result:
        return jsonify({"error": "Client to keep not found"}), 404
    return jsonify(result)


@crm_bp.route("/api/crm/clients/<int:client_id>/notes", methods=["POST"])
def crm_add_note(client_id):
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Note text is required"}), 400
    if not crm.get_client(client_id):
        abort(404)
    note = crm.add_note(client_id, text)
    return jsonify(note)


@crm_bp.route("/api/crm/clients/<int:client_id>/complete_followup", methods=["POST"])
def crm_complete_followup(client_id):
    client = crm.complete_followup(client_id)
    if not client:
        abort(404)
    return jsonify(client)


@crm_bp.route("/api/crm/clients/<int:client_id>/log_call", methods=["POST"])
def crm_log_call(client_id):
    client = crm.log_call(client_id)
    if not client:
        abort(404)
    return jsonify(client)


@crm_bp.route("/api/crm/clients/<int:client_id>/call_history", methods=["GET"])
def crm_call_history(client_id):
    if not crm.get_client(client_id):
        abort(404)
    return jsonify(crm.list_call_history(client_id))


@crm_bp.route("/api/crm/contact_lists", methods=["GET"])
def crm_list_contact_lists():
    return jsonify(crm.list_contact_lists())


@crm_bp.route("/api/crm/contact_lists", methods=["POST"])
def crm_create_contact_list():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        contact_list = crm.create_contact_list(
            name=name,
            frequency_key=data.get("frequency_key"),
            custom_amount=data.get("custom_amount"),
            custom_unit=data.get("custom_unit"),
        )
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid custom call schedule amount"}), 400
    return jsonify(contact_list)


@crm_bp.route("/api/crm/contact_lists/<int:list_id>", methods=["GET"])
def crm_get_contact_list(list_id):
    contact_list = crm.get_contact_list(list_id)
    if not contact_list:
        abort(404)
    return jsonify(contact_list)


@crm_bp.route("/api/crm/contact_lists/<int:list_id>", methods=["PUT"])
def crm_update_contact_list(list_id):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        contact_list = crm.update_contact_list(
            list_id,
            name=name,
            frequency_key=data.get("frequency_key"),
            custom_amount=data.get("custom_amount"),
            custom_unit=data.get("custom_unit"),
        )
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid custom call schedule amount"}), 400
    if not contact_list:
        abort(404)
    return jsonify(contact_list)


@crm_bp.route("/api/crm/contact_lists/<int:list_id>", methods=["DELETE"])
def crm_delete_contact_list(list_id):
    crm.delete_contact_list(list_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/contact_lists/<int:list_id>/members", methods=["POST"])
def crm_set_list_members(list_id):
    data = request.get_json(force=True)
    client_ids = data.get("client_ids") or []
    contact_list = crm.set_list_members(list_id, client_ids)
    if not contact_list:
        abort(404)
    return jsonify(contact_list)


@crm_bp.route("/api/crm/contact_lists/<int:list_id>/members/add", methods=["POST"])
def crm_add_list_members(list_id):
    data = request.get_json(force=True)
    client_ids = data.get("client_ids") or []
    contact_list = crm.add_list_members(list_id, client_ids)
    if not contact_list:
        abort(404)
    return jsonify(contact_list)


@crm_bp.route("/api/crm/contact_lists/<int:list_id>/members/<int:client_id>", methods=["DELETE"])
def crm_remove_list_member(list_id, client_id):
    contact_list = crm.remove_list_member(list_id, client_id)
    if not contact_list:
        abort(404)
    return jsonify(contact_list)


@crm_bp.route("/api/crm/contact_lists/<int:list_id>/complete_round", methods=["POST"])
def crm_complete_list_round(list_id):
    contact_list = crm.complete_list_round(list_id)
    if not contact_list:
        abort(404)
    return jsonify(contact_list)


@crm_bp.route("/api/crm/contact_lists/import_vcard", methods=["POST"])
def crm_import_vcard():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"error": "List name is required"}), 400
    address = (request.form.get("address") or "").strip()
    try:
        raw_text = request.files["file"].read().decode("utf-8", errors="replace")
        contact_list = crm.import_contact_list_file(
            name=name,
            address=address,
            frequency_key=request.form.get("frequency_key"),
            custom_amount=request.form.get("custom_amount"),
            custom_unit=request.form.get("custom_unit"),
            raw_text=raw_text,
        )
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid custom call schedule amount"}), 400
    if not contact_list:
        return jsonify({"error": "No contacts with both a name and phone number were found in that file."}), 400
    return jsonify(contact_list)


@crm_bp.route("/api/crm/import_recurring_csv/preview", methods=["POST"])
def crm_import_recurring_csv_preview():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    raw_text = request.files["file"].read().decode("utf-8", errors="replace")
    rows = crm.preview_recurring_followups_csv(raw_text)
    if rows is None:
        return jsonify({"error": "File doesn't match the expected recurring-follow-up export columns."}), 400
    return jsonify({"rows": rows})


@crm_bp.route("/api/crm/import_recurring_csv", methods=["POST"])
def crm_import_recurring_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    raw_text = request.files["file"].read().decode("utf-8", errors="replace")
    selected_indices = None
    raw_indices = request.form.get("selected_indices")
    if raw_indices is not None:
        try:
            selected_indices = set(json.loads(raw_indices))
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid selected_indices"}), 400
    result = crm.import_recurring_followups_csv(raw_text, selected_indices=selected_indices)
    return jsonify(result)


@crm_bp.route("/api/crm/events", methods=["POST"])
def crm_create_event():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    date_str = (data.get("date") or "").strip()
    if not title or not date_str:
        return jsonify({"error": "Title and date are required"}), 400
    client_id = data.get("client_id") or None
    if client_id and not crm.get_client(client_id):
        return jsonify({"error": "Client not found"}), 404
    list_id = data.get("list_id") or None
    if list_id and not crm.get_contact_list(list_id):
        return jsonify({"error": "Contact list not found"}), 404
    try:
        event = crm.create_event(
            client_id=client_id, list_id=list_id, title=title, date_str=date_str,
            time_str=(data.get("time") or "").strip(), notes=(data.get("notes") or "").strip(),
            address=(data.get("address") or "").strip(),
            meeting_type=(data.get("meeting_type") or "physical").strip(),
            meeting_provider=(data.get("meeting_provider") or "").strip() or None,
            attendee_email=(data.get("attendee_email") or "").strip() or None,
        )
    except RuntimeError as e:
        # create_virtual_meeting() raises this for anything meeting-
        # creation-specific (not connected, a real provider API failure)
        # -- nothing was written, so this is safe to just report back.
        return jsonify({"error": str(e)}), 502
    return jsonify(event)


@crm_bp.route("/api/crm/events/<int:event_id>", methods=["PUT"])
def crm_update_event(event_id):
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    date_str = (data.get("date") or "").strip()
    if not title or not date_str:
        return jsonify({"error": "Title and date are required"}), 400
    client_id = data.get("client_id") or None
    if client_id and not crm.get_client(client_id):
        return jsonify({"error": "Client not found"}), 404
    list_id = data.get("list_id") or None
    if list_id and not crm.get_contact_list(list_id):
        return jsonify({"error": "Contact list not found"}), 404
    try:
        event = crm.update_event(
            event_id, client_id=client_id, list_id=list_id, title=title, date_str=date_str,
            time_str=(data.get("time") or "").strip(), notes=(data.get("notes") or "").strip(),
            address=(data.get("address") or "").strip(),
            meeting_type=(data.get("meeting_type") or "physical").strip(),
            meeting_provider=(data.get("meeting_provider") or "").strip() or None,
            attendee_email=(data.get("attendee_email") or "").strip() or None,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    if not event:
        abort(404)
    return jsonify(event)


@crm_bp.route("/api/crm/events/<int:event_id>", methods=["DELETE"])
def crm_delete_event(event_id):
    crm.delete_event(event_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/followups/today")
def crm_followups_today():
    return jsonify(crm.get_today_followups())


@crm_bp.route("/api/crm/followups/missed")
def crm_followups_missed():
    return jsonify(crm.get_missed_followups())


@crm_bp.route("/api/backup/status")
def backup_status():
    return jsonify(crm.get_backup_status())


@crm_bp.route("/api/crm/meeting_providers/status")
def meeting_providers_status():
    return jsonify(crm.get_meeting_provider_status())


@crm_bp.route("/api/backup/run", methods=["POST"])
def backup_run():
    return jsonify(crm.backup_now("manual"))


@crm_bp.route("/api/backup/google_drive_dir", methods=["POST"])
def set_google_drive_dir():
    path = (request.get_json(force=True) or {}).get("path", "").strip()
    if not path:
        return jsonify({"error": "Enter a folder path."}), 400
    try:
        crm.set_google_drive_dir(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(crm.get_backup_status())


@crm_bp.route("/api/backup/health")
def backup_health():
    return jsonify(crm.check_data_health())


@crm_bp.route("/api/backup/snapshots")
def backup_snapshots():
    return jsonify(crm.list_available_snapshots())


@crm_bp.route("/api/backup/restore", methods=["POST"])
def backup_restore():
    path = (request.get_json(force=True) or {}).get("path", "").strip()
    if not path:
        return jsonify({"error": "No backup file specified."}), 400
    try:
        result = crm.restore_from_snapshot(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@crm_bp.route("/api/text_presets", methods=["GET"])
def get_text_presets():
    return jsonify(crm.list_text_presets())


@crm_bp.route("/api/text_presets", methods=["POST"])
def create_text_preset():
    text = (request.get_json(force=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "Type a message first."}), 400
    return jsonify(crm.add_text_preset(text)), 201


@crm_bp.route("/api/text_presets/<int:preset_id>", methods=["DELETE"])
def remove_text_preset(preset_id):
    crm.delete_text_preset(preset_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/calendar")
def crm_calendar():
    try:
        year = int(request.args.get("year"))
        month = int(request.args.get("month"))
    except (TypeError, ValueError):
        return jsonify({"error": "year and month are required"}), 400
    return jsonify(crm.get_calendar_events(year, month))


# ===================== Transaction Manager =====================

@crm_bp.route("/api/txn/types")
def txn_types():
    return jsonify([{"key": k, "label": label} for k, label in crm.TRANSACTION_TYPES])


@crm_bp.route("/api/txn/document_types")
def txn_document_types():
    txn_type = request.args.get("type", "")
    if txn_type not in crm.TRANSACTION_TYPE_KEYS:
        return jsonify({"error": "Unknown or missing transaction type"}), 400
    return jsonify(crm.list_document_types(txn_type))


@crm_bp.route("/api/txn/document_types", methods=["POST"])
def txn_add_document_type():
    data = request.get_json(force=True)
    txn_type = data.get("transaction_type", "")
    name = (data.get("name") or "").strip()
    if txn_type not in crm.TRANSACTION_TYPE_KEYS:
        return jsonify({"error": "Unknown or missing transaction type"}), 400
    if not name:
        return jsonify({"error": "Document name is required"}), 400
    return jsonify(crm.add_document_type(txn_type, name))


@crm_bp.route("/api/txn/document_types/<int:doc_type_id>", methods=["DELETE"])
def txn_delete_document_type(doc_type_id):
    crm.delete_document_type(doc_type_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/txn/document_types/<int:doc_type_id>/contract_flag", methods=["POST"])
def txn_set_document_type_contract_flag(doc_type_id):
    data = request.get_json(force=True)
    updated = crm.set_document_type_contract_flag(doc_type_id, bool(data.get("is_offer_contract")))
    if not updated:
        abort(404)
    return jsonify(updated)


@crm_bp.route("/api/txn/document_types/reorder", methods=["POST"])
def txn_reorder_document_types():
    data = request.get_json(force=True)
    txn_type = data.get("transaction_type", "")
    ordered_ids = data.get("ordered_ids") or []
    if txn_type not in crm.TRANSACTION_TYPE_KEYS:
        return jsonify({"error": "Unknown or missing transaction type"}), 400
    if not isinstance(ordered_ids, list):
        return jsonify({"error": "ordered_ids must be a list"}), 400
    return jsonify(crm.reorder_document_types(txn_type, ordered_ids))


@crm_bp.route("/api/txn/document_types/<int:doc_type_id>/template", methods=["POST"])
def txn_upload_document_type_template(doc_type_id):
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    updated = crm.set_document_type_template(doc_type_id, file)
    if not updated:
        abort(404)
    return jsonify(updated)


@crm_bp.route("/api/txn/document_types/<int:doc_type_id>/template", methods=["DELETE"])
def txn_remove_document_type_template(doc_type_id):
    crm.remove_document_type_template(doc_type_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/txn/document_types/<int:doc_type_id>/template")
def txn_download_document_type_template(doc_type_id):
    doc_type = crm.get_document_type(doc_type_id)
    if not doc_type or not doc_type["template_filename"]:
        abort(404)
    return send_file(
        crm.TEMPLATES_DIR / doc_type["template_filename"],
        # Inline by default so the browser previews PDFs/images in place
        # instead of downloading -- ?download=1 (the Download button/link
        # in the preview modal) still forces a real Save As.
        as_attachment=bool(request.args.get("download")),
        download_name=doc_type["template_original_name"] or "template",
    )


@crm_bp.route("/api/txn/transactions")
def txn_list_transactions():
    folder = request.args.get("folder", "active")
    return jsonify(crm.list_transactions(folder=folder))


@crm_bp.route("/api/txn/transactions", methods=["POST"])
def txn_create_transaction():
    data = request.get_json(force=True)
    txn_type = data.get("transaction_type", "")
    title = (data.get("title") or "").strip()
    if txn_type not in crm.TRANSACTION_TYPE_KEYS:
        return jsonify({"error": "Unknown or missing transaction type"}), 400
    if not title:
        return jsonify({"error": "Title is required"}), 400
    # represented_as is no longer its own field -- it's derived 1:1 from
    # the transaction type itself (see build order #117).
    return jsonify(crm.create_transaction(txn_type, title))


@crm_bp.route("/api/txn/transactions/<int:txn_id>")
def txn_get_transaction(txn_id):
    txn = crm.get_transaction(txn_id)
    if not txn:
        abort(404)
    return jsonify(txn)


@crm_bp.route("/api/txn/transactions/<int:txn_id>", methods=["PUT"])
def txn_update_transaction(txn_id):
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    status = data.get("status", "")
    if not title:
        return jsonify({"error": "Title is required"}), 400
    if status not in crm.TRANSACTION_STATUSES:
        return jsonify({"error": "Invalid status"}), 400
    sale_price = data.get("sale_price")
    commission_rate = data.get("commission_rate")
    # represented_as is derived once at creation from the transaction type
    # (see build order #117) and never independently edited afterward --
    # `update_transaction()` always keeps whatever's already on the row.
    updated = crm.update_transaction(
        txn_id, title, status,
        sale_price=float(sale_price) if sale_price not in (None, "") else None,
        commission_rate=float(commission_rate) if commission_rate not in (None, "") else None,
    )
    if not updated:
        abort(404)
    return jsonify(updated)


@crm_bp.route("/api/txn/transactions/<int:txn_id>", methods=["DELETE"])
def txn_delete_transaction(txn_id):
    crm.delete_transaction(txn_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/txn/transactions/<int:txn_id>/folder", methods=["POST"])
def txn_set_transaction_folder(txn_id):
    data = request.get_json(force=True)
    folder = data.get("folder", "")
    if folder not in crm.TRANSACTION_FOLDERS:
        return jsonify({"error": "Invalid folder"}), 400
    updated = crm.set_transaction_folder(txn_id, folder)
    if not updated:
        abort(404)
    return jsonify(updated)


@crm_bp.route("/api/txn/transaction_documents/<int:doc_id>/signed", methods=["POST"])
def txn_upload_signed(doc_id):
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    updated = crm.upload_signed_document(doc_id, file)
    if not updated:
        abort(404)
    return jsonify(updated)


@crm_bp.route("/api/txn/transaction_documents/<int:doc_id>/signed", methods=["DELETE"])
def txn_remove_signed(doc_id):
    updated = crm.remove_signed_document(doc_id)
    if not updated:
        abort(404)
    return jsonify(updated)


@crm_bp.route("/api/txn/transaction_documents/<int:doc_id>/signed")
def txn_download_signed(doc_id):
    row = crm.get_transaction_document(doc_id)
    if not row or not row["signed_filename"]:
        abort(404)
    return send_file(
        crm.SIGNED_DIR / row["signed_filename"],
        # Inline by default (in-app preview) -- ?download=1 forces a real
        # Save As, same convention as the other two file-serving routes.
        as_attachment=bool(request.args.get("download")),
        download_name=row["signed_original_name"] or "signed_document",
    )


# ===================== ShowingDay (GPS route optimization) =====================

@crm_bp.route("/api/crm/showing_days/ors_status")
def showing_ors_status():
    return jsonify(crm.get_ors_status())


@crm_bp.route("/api/crm/showing_days", methods=["GET"])
def showing_list_days():
    return jsonify(crm.list_showing_days(request.args.get("date") or None))


@crm_bp.route("/api/crm/showing_days", methods=["POST"])
def showing_create_day():
    data = request.get_json(force=True)
    date_str = (data.get("date") or "").strip()
    if not date_str:
        return jsonify({"error": "A date is required"}), 400
    day = crm.create_showing_day(
        date_str=date_str,
        lunch_enabled=bool(data.get("lunch_enabled", True)),
        lunch_start_time=(data.get("lunch_start_time") or "12:30").strip(),
        lunch_minutes=int(data.get("lunch_minutes") or 30),
    )
    return jsonify(day)


@crm_bp.route("/api/crm/showing_days/<int:day_id>", methods=["GET"])
def showing_get_day(day_id):
    day = crm.get_showing_day(day_id)
    if not day:
        abort(404)
    return jsonify(day)


@crm_bp.route("/api/crm/showing_days/<int:day_id>", methods=["PUT"])
def showing_update_day(day_id):
    data = request.get_json(force=True)
    day = crm.update_showing_day_settings(
        day_id,
        lunch_enabled=bool(data.get("lunch_enabled", True)),
        lunch_start_time=(data.get("lunch_start_time") or "12:30").strip(),
        lunch_minutes=int(data.get("lunch_minutes") or 30),
    )
    if not day:
        abort(404)
    return jsonify(day)


@crm_bp.route("/api/crm/showing_days/<int:day_id>", methods=["DELETE"])
def showing_delete_day(day_id):
    crm.delete_showing_day(day_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/showing_days/<int:day_id>/stops", methods=["POST"])
def showing_add_stop(day_id):
    data = request.get_json(force=True)
    address = (data.get("address") or "").strip()
    if not address:
        return jsonify({"error": "An address is required"}), 400
    try:
        stop = crm.add_showing_stop(
            day_id, address,
            client_id=data.get("client_id") or None,
            visit_minutes=int(data.get("visit_minutes") or 30),
            notes=(data.get("notes") or "").strip(),
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(stop)


@crm_bp.route("/api/crm/showing_stops/<int:stop_id>", methods=["DELETE"])
def showing_remove_stop(stop_id):
    crm.remove_showing_stop(stop_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/showing_days/<int:day_id>/optimize", methods=["POST"])
def showing_optimize(day_id):
    data = request.get_json(force=True)
    try:
        day = crm.optimize_showing_day(
            day_id,
            start_type=data.get("start_type"),
            start_address=data.get("start_address"),
            start_lat=data.get("start_lat"),
            start_lng=data.get("start_lng"),
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(day)


# ===================== EOS & Rocks =====================

@crm_bp.route("/api/crm/eos_rocks", methods=["GET"])
def eos_list_rocks():
    return jsonify(crm.get_eos_rocks())


@crm_bp.route("/api/crm/eos_rocks/<rock_key>", methods=["PUT"])
def eos_update_rock(rock_key):
    data = request.get_json(force=True)
    try:
        rock = crm.update_eos_rock(
            rock_key,
            title=(data.get("title") or "").strip(),
            description=(data.get("description") or "").strip(),
            start_date=(data.get("start_date") or "").strip() or None,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(rock)


# ===================== Waterfall to-do =====================

@crm_bp.route("/api/crm/waterfall_items", methods=["GET"])
def waterfall_list():
    return jsonify(crm.list_waterfall_items())


@crm_bp.route("/api/crm/waterfall_items", methods=["POST"])
def waterfall_add():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Enter a step first"}), 400
    return jsonify(crm.add_waterfall_item(title))


@crm_bp.route("/api/crm/waterfall_items/<int:item_id>", methods=["PUT"])
def waterfall_update(item_id):
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Enter a step first"}), 400
    updated = crm.update_waterfall_item(item_id, title)
    if not updated:
        abort(404)
    return jsonify(updated)


@crm_bp.route("/api/crm/waterfall_items/<int:item_id>", methods=["DELETE"])
def waterfall_delete(item_id):
    crm.delete_waterfall_item(item_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/waterfall_items/<int:item_id>/complete", methods=["POST"])
def waterfall_complete(item_id):
    data = request.get_json(force=True)
    try:
        item = crm.set_waterfall_item_completed(item_id, bool(data.get("completed", True)))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(item)


# ===================== Commission Tracker =====================

@crm_bp.route("/api/crm/commission_settings", methods=["GET"])
def commission_get_settings():
    return jsonify(crm.get_commission_settings())


@crm_bp.route("/api/crm/commission_settings", methods=["PUT"])
def commission_update_settings():
    data = request.get_json(force=True)
    if "default_split_pct" in data:
        crm.set_commission_default_split(float(data["default_split_pct"]))
    if "current_brokerage" in data:
        crm.set_commission_current_brokerage((data["current_brokerage"] or "").strip())
    return jsonify(crm.get_commission_settings())


@crm_bp.route("/api/crm/commission_entries", methods=["GET"])
def commission_list():
    sort = request.args.get("sort", "date_desc")
    limit = int(request.args.get("limit", 20))
    offset = int(request.args.get("offset", 0))
    return jsonify(crm.list_commission_entries(sort=sort, limit=limit, offset=offset))


@crm_bp.route("/api/crm/commission_entries/<int:entry_id>", methods=["GET"])
def commission_get(entry_id):
    entry = crm.get_commission_entry(entry_id)
    if not entry:
        abort(404)
    return jsonify(entry)


@crm_bp.route("/api/crm/commission_entries", methods=["POST"])
def commission_create():
    # multipart/form-data (not JSON) since this can include a file upload,
    # same shape as the Transaction Manager's signed-document upload route.
    title = (request.form.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400
    # Closing sale/rent amount, commission amount, and closing date are all
    # mandatory (Mario: "this is how it knows oldest or earliest," plus his
    # explicit ask for both the sale price AND the commission split to be
    # tracked separately) -- unlike the auto-added-on-close path (which
    # deliberately allows $0 for a transaction closed with no price set), a
    # MANUAL entry has no such excuse not to have real numbers.
    sale_price_raw = (request.form.get("sale_price") or "").strip()
    if not sale_price_raw:
        return jsonify({"error": "Closing sale amount is required"}), 400
    try:
        sale_price = float(sale_price_raw)
    except ValueError:
        return jsonify({"error": "Invalid closing sale amount"}), 400
    amount_raw = (request.form.get("amount") or "").strip()
    if not amount_raw:
        return jsonify({"error": "Commission amount is required"}), 400
    try:
        amount = float(amount_raw)
    except ValueError:
        return jsonify({"error": "Invalid amount"}), 400
    closed_date = (request.form.get("closed_date") or "").strip()
    if not closed_date:
        return jsonify({"error": "Closing date is required"}), 400
    split_pct = request.form.get("split_pct")
    entry = crm.create_commission_entry(
        title=title,
        description=(request.form.get("description") or "").strip(),
        amount=amount,
        sale_price=sale_price,
        split_pct=float(split_pct) if split_pct not in (None, "") else None,
        closed_date=closed_date,
        brokerage=(request.form.get("brokerage") or "").strip() or None,
        seller_names=(request.form.get("seller_names") or "").strip() or None,
        buyers_agent=(request.form.get("buyers_agent") or "").strip() or None,
        co_listing_agent=(request.form.get("co_listing_agent") or "").strip() or None,
        deal_type=(request.form.get("deal_type") or "sale").strip(),
        represented_as=(request.form.get("represented_as") or "seller").strip(),
        file_storage=request.files.get("file"),
    )
    return jsonify(entry)


@crm_bp.route("/api/crm/commission_entries/<int:entry_id>", methods=["PUT"])
def commission_update(entry_id):
    data = request.get_json(force=True)
    # seller_names/buyers_agent/co_listing_agent are sent as "" (not
    # omitted) when Mario clears one in the edit modal -- update_commission_entry()
    # treats an explicit "" as "clear this field" and None (key genuinely
    # absent) as "leave unchanged", so pass through .strip() results
    # directly rather than collapsing "" to None here.
    updated = crm.update_commission_entry(
        entry_id,
        title=(data.get("title") or "").strip() or None,
        description=data.get("description"),
        amount=float(data["amount"]) if data.get("amount") not in (None, "") else None,
        sale_price=float(data["sale_price"]) if data.get("sale_price") not in (None, "") else None,
        split_pct=float(data["split_pct"]) if data.get("split_pct") not in (None, "") else None,
        closed_date=(data.get("closed_date") or "").strip() or None,
        brokerage=(data.get("brokerage") or "").strip() or None,
        seller_names=data["seller_names"].strip() if "seller_names" in data else None,
        buyers_agent=data["buyers_agent"].strip() if "buyers_agent" in data else None,
        co_listing_agent=data["co_listing_agent"].strip() if "co_listing_agent" in data else None,
        deal_type=data.get("deal_type"),
        represented_as=data.get("represented_as"),
    )
    if not updated:
        abort(404)
    return jsonify(updated)


@crm_bp.route("/api/crm/commission_entries/<int:entry_id>", methods=["DELETE"])
def commission_delete(entry_id):
    crm.delete_commission_entry(entry_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/commission_entries/<int:entry_id>/file", methods=["POST"])
def commission_upload_file(entry_id):
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    updated = crm.set_commission_entry_file(entry_id, file)
    if not updated:
        abort(404)
    return jsonify(updated)


@crm_bp.route("/api/crm/commission_entries/<int:entry_id>/file", methods=["DELETE"])
def commission_remove_file(entry_id):
    updated = crm.remove_commission_entry_file(entry_id)
    if not updated:
        abort(404)
    return jsonify(updated)


@crm_bp.route("/api/crm/commission_entries/<int:entry_id>/file")
def commission_download_file(entry_id):
    entry = crm.get_commission_entry(entry_id)
    if not entry or not entry.get("file_filename"):
        abort(404)
    return send_file(
        crm.COMMISSION_FILES_DIR / entry["file_filename"],
        # Inline by default (in-app preview) -- ?download=1 forces a real
        # Save As, same convention as the other two file-serving routes.
        as_attachment=bool(request.args.get("download")),
        download_name=entry.get("file_original_name") or "attachment",
    )
