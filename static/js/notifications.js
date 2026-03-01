/**
 * NotifSystem — shared notification bell + history panel
 *
 * Usage (in each base template):
 *   NotifSystem.init({
 *     storageKey : 'spcheck_notifs_<personnelId>',
 *     sseUrl     : '/api/rfid/notifications/<personnelId>'   // or hr/vp endpoint
 *   });
 *
 * Attendance pages dispatch:
 *   window.dispatchEvent(new CustomEvent('spcheck-notification', { detail: data }));
 * instead of opening their own EventSource.
 */
const NotifSystem = (function () {

  const MAX_STORED = 100;
  let _storageKey = '';
  let _filter = 'all';
  let _es = null;

  // DOM refs
  let elPanel, elOverlay, elBadge, elList;

  /* ------------------------------------------------------------------ */
  /*  Public API                                                          */
  /* ------------------------------------------------------------------ */

  function init(cfg) {
    _storageKey = cfg.storageKey;

    elPanel   = document.getElementById('notifPanel');
    elOverlay = document.getElementById('notifOverlay');
    elBadge   = document.getElementById('notifBadge');
    elList    = document.getElementById('notifList');

    if (!elPanel || !elOverlay || !elBadge || !elList) return;

    document.getElementById('notifBellBtn').addEventListener('click', toggle);
    document.getElementById('notifCloseBtn').addEventListener('click', close);
    elOverlay.addEventListener('click', close);
    document.getElementById('notifMarkAllBtn').addEventListener('click', markAllRead);
    document.getElementById('notifClearBtn').addEventListener('click', clearAll);

    document.querySelectorAll('.notif-filter-btn').forEach(function (btn) {
      btn.addEventListener('click', function () { setFilter(btn.dataset.filter); });
    });

    window.addEventListener('spcheck-notification', function (e) {
      _store(e.detail);
    });

    _render();
    _updateBadge();

    // Load persisted notifications from the server (cross-session / cross-device)
    _syncFromServer();

    if (cfg.sseUrl) {
      _startSSE(cfg.sseUrl);
    }
  }

  function toggle() {
    elPanel.classList.contains('open') ? close() : open();
  }

  function open() {
    elPanel.classList.add('open');
    elOverlay.classList.add('show');
    setTimeout(_markAllRead, 1200);
  }

  function close() {
    elPanel.classList.remove('open');
    elOverlay.classList.remove('show');
  }

  function setFilter(f) {
    _filter = f;
    document.querySelectorAll('.notif-filter-btn').forEach(function (btn) {
      btn.classList.toggle('active', btn.dataset.filter === f);
    });
    _render();
  }

  function markAllRead() {
    _markAllRead();
    _render();
  }

  function clearAll() {
    _save([]);
    _render();
    _updateBadge();
    // Sync clear to server so other sessions reflect it
    fetch('/api/notifications/clear', { method: 'POST' }).catch(function () {});
  }

  /* ------------------------------------------------------------------ */
  /*  SSE                                                                 */
  /* ------------------------------------------------------------------ */

  function _startSSE(url) {
    if (_es) { _es.close(); }
    _es = new EventSource(url);

    _es.onmessage = function (event) {
      try {
        var data = JSON.parse(event.data);
        if (data.tap_time) {
          window.dispatchEvent(new CustomEvent('spcheck-notification', { detail: data }));
        }
      } catch (err) { /* ignore */ }
    };

    _es.onerror = function () {
      _es.close();
      _es = null;
      setTimeout(function () { _startSSE(url); }, 5000);
    };
  }

  /* ------------------------------------------------------------------ */
  /*  Storage — keep ALL raw fields so panel can show full detail        */
  /* ------------------------------------------------------------------ */

  function _store(data) {
    if (!data) return;
    if (!data.tap_time && data.notification_type !== 'license') return;

    var notifs = _load();

    // Use the DB-assigned notif_id when available so we can deduplicate on reload
    var itemId = data.notif_id ? 'db_' + data.notif_id
                               : (Date.now() + '_' + Math.random().toString(36).slice(2));

    // Skip if this DB record is already in localStorage (e.g. loaded by _syncFromServer)
    if (data.notif_id && notifs.some(function (n) { return n.id === itemId; })) return;

    var item = {
      id            : Date.now() + '_' + Math.random().toString(36).slice(2),
      // sensor / alert type
      type          : data.notification_type || 'rfid',
      // identity
      person_name   : data.person_name  || 'Unknown',
      personnel_id  : data.personnel_id || null,
      rfid_uid      : data.rfid_uid     || null,
      biometric_uid : data.biometric_uid || null,
      biometric_id  : data.biometric_id  || null,
      // action / result
      action        : data.action  || '',
      status        : data.status  || '',
      message       : data.message || '',
      // class details (RFID)
      subject_code  : data.subject_code  || null,
      subject_name  : data.subject_name  || null,
      class_section : data.class_section || null,
      classroom     : data.classroom     || null,
      // license details
      license_type       : data.license_type        || null,
      license_number     : data.license_number      || null,
      expiration_date    : data.expiration_date      || null,
      days_until_expiry  : data.days_until_expiry != null ? data.days_until_expiry : null,
      // timestamp
      tap_time      : data.tap_time || new Date().toLocaleString('en-US', { weekday:'long', year:'numeric', month:'long', day:'numeric', hour:'numeric', minute:'2-digit', hour12:true }),
      // panel state
      read          : false,
      ts            : Date.now()
    };

    notifs.unshift(item);
    if (notifs.length > MAX_STORED) { notifs = notifs.slice(0, MAX_STORED); }
    _save(notifs);
    _render();
    _updateBadge();
  }

  function _load() {
    try { return JSON.parse(localStorage.getItem(_storageKey) || '[]'); }
    catch (e) { return []; }
  }

  function _save(notifs) {
    try { localStorage.setItem(_storageKey, JSON.stringify(notifs)); }
    catch (e) { /* storage full */ }
  }

  /* ------------------------------------------------------------------ */
  /*  Read state                                                          */
  /* ------------------------------------------------------------------ */

  function _markAllRead() {
    var notifs = _load().map(function (n) { return Object.assign({}, n, { read: true }); });
    _save(notifs);
    _updateBadge();
    document.querySelectorAll('.notif-unread-dot').forEach(function (d) { d.style.display = 'none'; });
    document.querySelectorAll('.notif-item.unread').forEach(function (el) { el.classList.remove('unread'); });
    // Sync read state to server so other devices see it
    fetch('/api/notifications/mark-read', { method: 'POST' }).catch(function () {});
  }

  /* ------------------------------------------------------------------ */
  /*  Server sync — load persisted notifications on page load           */
  /* ------------------------------------------------------------------ */

  function _syncFromServer() {
    fetch('/api/notifications/history')
      .then(function (r) { return r.json(); })
      .then(function (resp) {
        if (!resp.success || !Array.isArray(resp.notifications)) return;

        // Map DB rows to the local notification shape
        var serverNotifs = resp.notifications.map(function (n) {
          return {
            id            : 'db_' + n.notif_id,
            type          : n.notification_type || 'rfid',
            person_name   : n.person_name  || 'Unknown',
            personnel_id  : n.personnel_id || null,
            rfid_uid      : n.rfid_uid     || null,
            biometric_uid : n.biometric_uid || null,
            biometric_id  : n.biometric_id  || null,
            action        : n.action  || '',
            status        : n.status  || '',
            message       : n.message || '',
            subject_code  : n.subject_code  || null,
            subject_name  : n.subject_name  || null,
            class_section : n.class_section || null,
            classroom     : n.classroom     || null,
            tap_time      : n.tap_time,
            read          : n.is_read,
            ts            : n.created_at ? new Date(n.created_at).getTime() : Date.now()
          };
        });

        // Keep any local-only items (no db_ prefix) that arrived via SSE before sync completed
        var localOnly = _load().filter(function (n) { return n.id.indexOf('db_') !== 0; });
        var merged = serverNotifs.concat(localOnly);
        merged.sort(function (a, b) { return b.ts - a.ts; });
        if (merged.length > MAX_STORED) { merged = merged.slice(0, MAX_STORED); }

        _save(merged);
        _render();
        _updateBadge();
      })
      .catch(function () { /* silently fall back to localStorage */ });
  }

  function _updateBadge() {
    var count = _load().filter(function (n) { return !n.read; }).length;
    elBadge.textContent = count > 99 ? '99+' : String(count);
    elBadge.style.display = count > 0 ? '' : 'none';
  }

  /* ------------------------------------------------------------------ */
  /*  Title / icon / colour helpers — match the original toast cards     */
  /* ------------------------------------------------------------------ */

  function _rfidMeta(n) {
    var title, icon, color;

    if (n.action === 'unknown_rfid') {
      title = 'Unknown RFID Card';       icon = 'bx-error-circle';   color = '#ef4444';
    } else if (n.action === 'no_schedule') {
      title = 'No Teaching Load';        icon = 'bx-calendar-x';     color = '#f59e0b';
    } else if (n.action === 'outside_buffer' || n.status === 'outside_buffer') {
      title = 'Outside Class Schedule';  icon = 'bx-time';            color = '#f59e0b';
    } else if (n.action === 'timein') {
      title = 'Time-In Recorded';        icon = 'bx-log-in-circle';
      color = (n.status === 'Present') ? '#16a34a' : '#f59e0b';
    } else if (n.action === 'timeout') {
      title = 'Time-Out Recorded';       icon = 'bx-log-out-circle';  color = '#16a34a';
    } else if (n.action === 'buffer_period') {
      title = 'Within Buffer Period';    icon = 'bx-time-five';       color = '#3b82f6';
    } else if (n.action === 'duplicate_timein') {
      title = 'Already Timed-In';        icon = 'bx-error-circle';    color = '#3b82f6';
    } else if (n.action === 'duplicate_timeout') {
      title = 'Already Timed-Out';       icon = 'bx-error-circle';    color = '#3b82f6';
    } else if (n.action === 'already_complete') {
      title = 'Attendance Complete';     icon = 'bx-check-double';    color = '#6b7280';
    } else if (n.action === 'no_timein') {
      title = 'No Time-In Found';        icon = 'bx-error-alt';       color = '#ef4444';
    } else if (n.action === 'duplicate') {
      title = 'Already Recorded';        icon = 'bx-error-circle';    color = '#3b82f6';
    } else {
      title = 'RFID Tapped';             icon = 'bx-wifi-2';          color = '#6b7280';
    }
    return { title: title, icon: icon, color: color };
  }

  function _bioMeta(n) {
    var title, icon, color;

    if (n.action === 'entry') {
      title = 'Entry Recorded';      icon = 'bx-log-in-circle';  color = '#16a34a';
    } else if (n.action === 'exit') {
      title = 'Exit Recorded';       icon = 'bx-log-out-circle'; color = '#16a34a';
    } else if (n.action === 'buffer_period') {
      title = 'Already Scanned';     icon = 'bx-time';           color = '#3b82f6';
    } else if (n.action === 'unknown_biometric') {
      title = 'Unknown Fingerprint'; icon = 'bx-error-circle';   color = '#ef4444';
    } else {
      title = 'Biometric Scanned';   icon = 'bx-fingerprint';    color = '#6b7280';
    }
    return { title: title, icon: icon, color: color };
  }

  function _licenseMeta(n) {
    var title, icon, color;
    if (n.action === 'expired') {
      title = 'License Expired';       icon = 'bx-error-circle'; color = '#dc2626';
    } else if (n.action === 'expiring_30') {
      title = 'License Expiring Soon'; icon = 'bx-error';        color = '#ea580c';
    } else if (n.action === 'expiring_60') {
      title = 'License Expiring';      icon = 'bx-bell';         color = '#d97706';
    } else {
      title = 'License Reminder';      icon = 'bx-bell';         color = '#ca8a04';
    }
    return { title: title, icon: icon, color: color };
  }

  /* ------------------------------------------------------------------ */
  /*  Timestamp formatter                                                 */
  /* ------------------------------------------------------------------ */

  function _formatTime(tapTime) {
    try {
      var parts = tapTime.split(', ');
      var datePart = parts.length > 1 ? parts.slice(1).join(', ') : tapTime;
      var d = new Date(datePart.replace(' ', 'T'));
      if (isNaN(d.getTime())) return tapTime;
      var weekday = d.toLocaleString('en-US', { weekday: 'long' });
      var month   = d.toLocaleString('en-US', { month: 'long' });
      var day     = d.getDate();
      var year    = d.getFullYear();
      var time    = d.toLocaleString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
      return weekday + ' ' + month + ' ' + day + ', ' + year + ' ' + time;
    } catch (e) { return tapTime; }
  }

  /* ------------------------------------------------------------------ */
  /*  Render                                                              */
  /* ------------------------------------------------------------------ */

  function _render() {
    if (!elList) return;

    var notifs = _load();
    if (_filter !== 'all') {
      notifs = notifs.filter(function (n) { return n.type === _filter; });
    }

    if (notifs.length === 0) {
      elList.innerHTML =
        '<div class="notif-empty">' +
          '<i class="bx bx-bell-off"></i>' +
          'No notifications yet' +
        '</div>';
      return;
    }

    var html = notifs.map(function (n) {
      var isBio     = (n.type === 'biometric');
      var isLicense = (n.type === 'license');
      var meta      = isLicense ? _licenseMeta(n) : (isBio ? _bioMeta(n) : _rfidMeta(n));

      // Type tag styling
      var tagBg    = isLicense ? '#fef3c7' : (isBio ? '#dcfce7'  : '#dbeafe');
      var tagColor = isLicense ? '#92400e' : (isBio ? '#166534'  : '#1e40af');
      var tagLabel = isLicense ? 'License' : (isBio ? 'Biometric' : 'RFID');

      // Icon background tinted from action colour
      var iconBg = meta.color + '18'; // 9% opacity hex

      // Unread dot
      var dot = n.read
        ? '<span class="notif-unread-dot" style="display:none"></span>'
        : '<span class="notif-unread-dot"></span>';

      // ---- Build detail rows ----
      var details = '';

      // Person name (skip for RFID unknown which has no person)
      if (n.person_name && n.person_name !== 'Unknown') {
        details += '<div class="notif-item-person">' + _esc(n.person_name) + '</div>';
      } else if (n.person_name === 'Unknown') {
        details += '<div class="notif-item-person" style="color:#9ca3af;font-style:italic">Unknown person</div>';
      }

      // RFID UID (shown for unknown RFID)
      if (!isBio && n.action === 'unknown_rfid' && n.rfid_uid) {
        details += '<div class="notif-item-detail"><span class="notif-detail-label">RFID UID</span>' + _esc(n.rfid_uid) + '</div>';
      }

      // Subject (RFID)
      if (!isBio && n.subject_code && n.subject_name) {
        details += '<div class="notif-item-detail notif-item-subject">' +
          '<i class="bx bx-book-open"></i> ' +
          '<strong>' + _esc(n.subject_code) + '</strong> — ' + _esc(n.subject_name) +
        '</div>';
      }

      // Section + Room (RFID)
      if (!isBio && (n.class_section || n.classroom)) {
        var loc = [];
        if (n.class_section) loc.push('<span><i class="bx bx-group"></i> Section: <strong>' + _esc(n.class_section) + '</strong></span>');
        if (n.classroom)     loc.push('<span><i class="bx bx-door-open"></i> Room: <strong>' + _esc(n.classroom) + '</strong></span>');
        details += '<div class="notif-item-detail notif-item-location">' + loc.join(' &bull; ') + '</div>';
      }

      // Attendance status badge (Present / Late)
      if (!isBio && (n.action === 'timein' || n.action === 'timeout') &&
          n.status && n.status !== 'outside_buffer' && n.status !== 'no_schedule' &&
          n.status !== 'error' && n.status !== 'warning') {
        var sbColor = (n.status === 'Present') ? { bg: '#d1fae5', text: '#065f46' } :
                      (n.status === 'Late')    ? { bg: '#fef3c7', text: '#92400e' } :
                                                 { bg: '#e5e7eb', text: '#374151' };
        details += '<div class="notif-item-detail">' +
          'Status: <span style="display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;background:' +
          sbColor.bg + ';color:' + sbColor.text + '">' + _esc(n.status) + '</span>' +
        '</div>';
      }

      // Biometric entry/exit direction badge
      if (isBio && (n.action === 'entry' || n.action === 'exit')) {
        var bDir = n.action === 'entry'
          ? { bg: '#d1fae5', text: '#065f46', label: 'Entry' }
          : { bg: '#dbeafe', text: '#1e40af', label: 'Exit'  };
        details += '<div class="notif-item-detail">' +
          'Direction: <span style="display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;background:' +
          bDir.bg + ';color:' + bDir.text + '">' + bDir.label + '</span>' +
        '</div>';
      }

      // License-specific detail rows
      if (isLicense) {
        if (n.license_type)   details += '<div class="notif-item-detail"><span class="notif-detail-label">Type</span>' + _esc(n.license_type) + '</div>';
        if (n.license_number) details += '<div class="notif-item-detail"><span class="notif-detail-label">No.</span>' + _esc(n.license_number) + '</div>';
        if (n.expiration_date) {
          var dLabel = n.days_until_expiry != null
            ? (n.days_until_expiry < 0 ? ' (Expired)' : ' (' + n.days_until_expiry + ' day(s) left)')
            : '';
          details += '<div class="notif-item-detail"><span class="notif-detail-label">Expires</span>' + _esc(n.expiration_date) + dLabel + '</div>';
        }
      }

      // Message (always shown)
      if (n.message) {
        details += '<div class="notif-item-msg">' + _esc(n.message) + '</div>';
      }

      // Timestamp
      details += '<div class="notif-item-time">' + _formatTime(n.tap_time) + '</div>';

      // ---- Assemble item ----
      return '<div class="notif-item' + (n.read ? '' : ' unread') + '" data-id="' + n.id + '">' +
        dot +
        '<div class="notif-item-icon" style="background:' + iconBg + '">' +
          '<i class="bx ' + meta.icon + '" style="color:' + meta.color + '"></i>' +
        '</div>' +
        '<div class="notif-item-body">' +
          '<div class="notif-item-header-row">' +
            '<span class="notif-type-tag" style="background:' + tagBg + ';color:' + tagColor + '">' + tagLabel + '</span>' +
            '<span class="notif-item-title" style="color:' + meta.color + '">' + meta.title + '</span>' +
          '</div>' +
          details +
        '</div>' +
      '</div>';

    }).join('');

    elList.innerHTML = html;
  }

  function _esc(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  return {
    init       : init,
    toggle     : toggle,
    open       : open,
    close      : close,
    setFilter  : setFilter,
    markAllRead: markAllRead,
    clearAll   : clearAll
  };

})();
