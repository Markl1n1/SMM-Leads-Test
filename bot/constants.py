(
    # Check states
    CHECK_BY_TELEGRAM,
    CHECK_BY_FB_LINK,
    CHECK_BY_TELEGRAM_ID,
    CHECK_BY_FULLNAME,
    SMART_CHECK_INPUT,  # Smart check with auto-detection
    # Add states (sequential flow)
    ADD_FULLNAME,
    ADD_MANAGER_NAME,
    ADD_FB_LINK,
    ADD_TELEGRAM_NAME,
    ADD_TELEGRAM_ID,
    ADD_REVIEW,  # Review before saving
    # Edit states
    EDIT_MENU,
    EDIT_FULLNAME,
    EDIT_FB_LINK,
    EDIT_TELEGRAM_NAME,
    EDIT_TELEGRAM_ID,
    EDIT_MANAGER_NAME,
    EDIT_PIN,
    # Tag states
    TAG_PIN,
    TAG_SELECT_MANAGER,
    TAG_ENTER_NEW,
    # Transfer states
    TRANSFER_PIN,
    TRANSFER_SELECT_FROM,
    TRANSFER_SELECT_TO,
    TRANSFER_CONFIRM
) = range(25)

