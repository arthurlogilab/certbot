"""Let's Encrypt main entry point."""
from __future__ import print_function
import atexit
import functools
import os
import sys
import zope.component

import letsencrypt

from letsencrypt import account
from letsencrypt import client
from letsencrypt import cli
from letsencrypt import crypto_util
from letsencrypt import colored_logging
from letsencrypt import configuration
from letsencrypt import constants
from letsencrypt import errors
from letsencrypt import interfaces
from letsencrypt import le_util
from letsencrypt import log
from letsencrypt import reporter
from letsencrypt import storage

from letsencrypt.cli import choose_configurator_plugins, _renewal_conf_files, should_renew
from letsencrypt.display import util as display_util, ops as display_ops
from letsencrypt.plugins import disco as plugins_disco

import traceback
import logging.handlers
import time
from acme import jose
import OpenSSL

logger = logging.getLogger(__name__)


def _suggest_donation_if_appropriate(config, action):
    """Potentially suggest a donation to support Let's Encrypt."""
    if config.staging or config.verb == "renew":
        # --dry-run implies --staging
        return
    if action not in ["renew", "newcert"]:
        return
    reporter_util = zope.component.getUtility(interfaces.IReporter)
    msg = ("If you like Let's Encrypt, please consider supporting our work by:\n\n"
           "Donating to ISRG / Let's Encrypt:   https://letsencrypt.org/donate\n"
           "Donating to EFF:                    https://eff.org/donate-le\n\n")
    reporter_util.add_message(msg, reporter_util.LOW_PRIORITY)


def _avoid_invalidating_lineage(config, lineage, original_server):
    "Do not renew a valid cert with one from a staging server!"
    def _is_staging(srv):
        return srv == constants.STAGING_URI or "staging" in srv

    # Some lineages may have begun with --staging, but then had production certs
    # added to them
    latest_cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                                  open(lineage.cert).read())
    # all our test certs are from happy hacker fake CA, though maybe one day
    # we should test more methodically
    now_valid = "fake" not in repr(latest_cert.get_issuer()).lower()

    if _is_staging(config.server):
        if not _is_staging(original_server) or now_valid:
            if not config.break_my_certs:
                names = ", ".join(lineage.names())
                raise errors.Error(
                    "You've asked to renew/replace a seemingly valid certificate with "
                    "a test certificate (domains: {0}). We will not do that "
                    "unless you use the --break-my-certs flag!".format(names))


def _report_successful_dry_run(config):
    reporter_util = zope.component.getUtility(interfaces.IReporter)
    if config.verb != "renew":
        reporter_util.add_message("The dry run was successful.",
                                  reporter_util.HIGH_PRIORITY, on_crash=False)


def _auth_from_domains(le_client, config, domains, lineage=None):
    """Authenticate and enroll certificate."""
    # Note: This can raise errors... caught above us though. This is now
    # a three-way case: reinstall (which results in a no-op here because
    # although there is a relevant lineage, we don't do anything to it
    # inside this function -- we don't obtain a new certificate), renew
    # (which results in treating the request as a renewal), or newcert
    # (which results in treating the request as a new certificate request).

    # If lineage is specified, use that one instead of looking around for
    # a matching one.
    if lineage is None:
        # This will find a relevant matching lineage that exists
        action, lineage = _treat_as_renewal(config, domains)
    else:
        # Renewal, where we already know the specific lineage we're
        # interested in
        action = "renew"

    if action == "reinstall":
        # The lineage already exists; allow the caller to try installing
        # it without getting a new certificate at all.
        return lineage, "reinstall"
    elif action == "renew":
        original_server = lineage.configuration["renewalparams"]["server"]
        _avoid_invalidating_lineage(config, lineage, original_server)
        # TODO: schoen wishes to reuse key - discussion
        # https://github.com/letsencrypt/letsencrypt/pull/777/files#r40498574
        new_certr, new_chain, new_key, _ = le_client.obtain_certificate(domains)
        # TODO: Check whether it worked! <- or make sure errors are thrown (jdk)
        if config.dry_run:
            logger.info("Dry run: skipping updating lineage at %s",
                        os.path.dirname(lineage.cert))
        else:
            lineage.save_successor(
                lineage.latest_common_version(), OpenSSL.crypto.dump_certificate(
                    OpenSSL.crypto.FILETYPE_PEM, new_certr.body.wrapped),
                new_key.pem, crypto_util.dump_pyopenssl_chain(new_chain),
                configuration.RenewerConfiguration(config.namespace))
            lineage.update_all_links_to(lineage.latest_common_version())
        # TODO: Check return value of save_successor
        # TODO: Also update lineage renewal config with any relevant
        #       configuration values from this attempt? <- Absolutely (jdkasten)
    elif action == "newcert":
        # TREAT AS NEW REQUEST
        lineage = le_client.obtain_and_enroll_certificate(domains)
        if lineage is False:
            raise errors.Error("Certificate could not be obtained")

    if not config.dry_run and not config.verb == "renew":
        _report_new_cert(lineage.cert, lineage.fullchain)

    return lineage, action


def _handle_subset_cert_request(config, domains, cert):
    """Figure out what to do if a previous cert had a subset of the names now requested

    :param storage.RenewableCert cert:

    :returns: Tuple of (string, cert_or_None) as per _treat_as_renewal
    :rtype: tuple

    """
    existing = ", ".join(cert.names())
    question = (
        "You have an existing certificate that contains a portion of "
        "the domains you requested (ref: {0}){br}{br}It contains these "
        "names: {1}{br}{br}You requested these names for the new "
        "certificate: {2}.{br}{br}Do you want to expand and replace this existing "
        "certificate with the new certificate?"
    ).format(cert.configfile.filename,
             existing,
             ", ".join(domains),
             br=os.linesep)
    if config.expand or config.renew_by_default or zope.component.getUtility(
            interfaces.IDisplay).yesno(question, "Expand", "Cancel",
                                       cli_flag="--expand (or in some cases, --duplicate)"):
        return "renew", cert
    else:
        reporter_util = zope.component.getUtility(interfaces.IReporter)
        reporter_util.add_message(
            "To obtain a new certificate that contains these names without "
            "replacing your existing certificate for {0}, you must use the "
            "--duplicate option.{br}{br}"
            "For example:{br}{br}{1} --duplicate {2}".format(
                existing,
                sys.argv[0], " ".join(sys.argv[1:]),
                br=os.linesep
            ),
            reporter_util.HIGH_PRIORITY)
        raise errors.Error(
            "User chose to cancel the operation and may "
            "reinvoke the client.")


def _handle_identical_cert_request(config, cert):
    """Figure out what to do if a cert has the same names as a previously obtained one

    :param storage.RenewableCert cert:

    :returns: Tuple of (string, cert_or_None) as per _treat_as_renewal
    :rtype: tuple

    """
    if should_renew(config, cert):
        return "renew", cert
    if config.reinstall:
        # Set with --reinstall, force an identical certificate to be
        # reinstalled without further prompting.
        return "reinstall", cert
    question = (
        "You have an existing certificate that contains exactly the same "
        "domains you requested and isn't close to expiry."
        "{br}(ref: {0}){br}{br}What would you like to do?"
    ).format(cert.configfile.filename, br=os.linesep)

    if config.verb == "run":
        keep_opt = "Attempt to reinstall this existing certificate"
    elif config.verb == "certonly":
        keep_opt = "Keep the existing certificate for now"
    choices = [keep_opt,
               "Renew & replace the cert (limit ~5 per 7 days)"]

    display = zope.component.getUtility(interfaces.IDisplay)
    response = display.menu(question, choices, "OK", "Cancel", default=0)
    if response[0] == display_util.CANCEL:
        # TODO: Add notification related to command-line options for
        #       skipping the menu for this case.
        raise errors.Error(
            "User chose to cancel the operation and may "
            "reinvoke the client.")
    elif response[1] == 0:
        return "reinstall", cert
    elif response[1] == 1:
        return "renew", cert
    else:
        assert False, "This is impossible"


def _treat_as_renewal(config, domains):
    """Determine whether there are duplicated names and how to handle
    them (renew, reinstall, newcert, or raising an error to stop
    the client run if the user chooses to cancel the operation when
    prompted).

    :returns: Two-element tuple containing desired new-certificate behavior as
              a string token ("reinstall", "renew", or "newcert"), plus either
              a RenewableCert instance or None if renewal shouldn't occur.

    :raises .Error: If the user would like to rerun the client again.

    """
    # Considering the possibility that the requested certificate is
    # related to an existing certificate.  (config.duplicate, which
    # is set with --duplicate, skips all of this logic and forces any
    # kind of certificate to be obtained with renewal = False.)
    if config.duplicate:
        return "newcert", None
    # TODO: Also address superset case
    ident_names_cert, subset_names_cert = _find_duplicative_certs(config, domains)
    # XXX ^ schoen is not sure whether that correctly reads the systemwide
    # configuration file.
    if ident_names_cert is None and subset_names_cert is None:
        return "newcert", None

    if ident_names_cert is not None:
        return _handle_identical_cert_request(config, ident_names_cert)
    elif subset_names_cert is not None:
        return _handle_subset_cert_request(config, domains, subset_names_cert)


def _find_duplicative_certs(config, domains):
    """Find existing certs that duplicate the request."""

    identical_names_cert, subset_names_cert = None, None

    cli_config = configuration.RenewerConfiguration(config)
    configs_dir = cli_config.renewal_configs_dir
    # Verify the directory is there
    le_util.make_or_verify_dir(configs_dir, mode=0o755, uid=os.geteuid())

    for renewal_file in _renewal_conf_files(cli_config):
        try:
            candidate_lineage = storage.RenewableCert(renewal_file, cli_config)
        except (errors.CertStorageError, IOError):
            logger.warning("Renewal conf file %s is broken. Skipping.", renewal_file)
            logger.debug("Traceback was:\n%s", traceback.format_exc())
            continue
        # TODO: Handle these differently depending on whether they are
        #       expired or still valid?
        candidate_names = set(candidate_lineage.names())
        if candidate_names == set(domains):
            identical_names_cert = candidate_lineage
        elif candidate_names.issubset(set(domains)):
            # This logic finds and returns the largest subset-names cert
            # in the case where there are several available.
            if subset_names_cert is None:
                subset_names_cert = candidate_lineage
            elif len(candidate_names) > len(subset_names_cert.names()):
                subset_names_cert = candidate_lineage

    return identical_names_cert, subset_names_cert


def _find_domains(config, installer):
    if not config.domains:
        domains = display_ops.choose_names(installer)
        # record in config.domains (so that it can be serialised in renewal config files),
        # and set webroot_map entries if applicable
        for d in domains:
            cli.process_domain(config, d)
    else:
        domains = config.domains

    if not domains:
        raise errors.Error("Please specify --domains, or --installer that "
                           "will help in domain names autodiscovery")

    return domains


def _report_new_cert(cert_path, fullchain_path):
    """Reports the creation of a new certificate to the user.

    :param str cert_path: path to cert
    :param str fullchain_path: path to full chain

    """
    expiry = crypto_util.notAfter(cert_path).date()
    reporter_util = zope.component.getUtility(interfaces.IReporter)
    if fullchain_path:
        # Print the path to fullchain.pem because that's what modern webservers
        # (Nginx and Apache2.4) will want.
        and_chain = "and chain have"
        path = fullchain_path
    else:
        # Unless we're in .csr mode and there really isn't one
        and_chain = "has "
        path = cert_path
    # XXX Perhaps one day we could detect the presence of known old webservers
    # and say something more informative here.
    msg = ("Congratulations! Your certificate {0} been saved at {1}."
           " Your cert will expire on {2}. To obtain a new version of the "
           "certificate in the future, simply run Let's Encrypt again."
           .format(and_chain, path, expiry))
    reporter_util.add_message(msg, reporter_util.MEDIUM_PRIORITY)


def _determine_account(config):
    """Determine which account to use.

    In order to make the renewer (configuration de/serialization) happy,
    if ``config.account`` is ``None``, it will be updated based on the
    user input. Same for ``config.email``.

    :param argparse.Namespace config: CLI arguments
    :param letsencrypt.interface.IConfig config: Configuration object
    :param .AccountStorage account_storage: Account storage.

    :returns: Account and optionally ACME client API (biproduct of new
        registration).
    :rtype: `tuple` of `letsencrypt.account.Account` and
        `acme.client.Client`

    """
    account_storage = account.AccountFileStorage(config)
    acme = None

    if config.account is not None:
        acc = account_storage.load(config.account)
    else:
        accounts = account_storage.find_all()
        if len(accounts) > 1:
            acc = display_ops.choose_account(accounts)
        elif len(accounts) == 1:
            acc = accounts[0]
        else:  # no account registered yet
            if config.email is None and not config.register_unsafely_without_email:
                config.namespace.email = display_ops.get_email()

            def _tos_cb(regr):
                if config.tos:
                    return True
                msg = ("Please read the Terms of Service at {0}. You "
                       "must agree in order to register with the ACME "
                       "server at {1}".format(
                           regr.terms_of_service, config.server))
                obj = zope.component.getUtility(interfaces.IDisplay)
                return obj.yesno(msg, "Agree", "Cancel", cli_flag="--agree-tos")

            try:
                acc, acme = client.register(
                    config, account_storage, tos_cb=_tos_cb)
            except errors.MissingCommandlineFlag:
                raise
            except errors.Error as error:
                logger.debug(error, exc_info=True)
                raise errors.Error(
                    "Unable to register an account with ACME server")

    config.namespace.account = acc.id
    return acc, acme


def _init_le_client(config, authenticator, installer):
    if authenticator is not None:
        # if authenticator was given, then we will need account...
        acc, acme = _determine_account(config)
        logger.debug("Picked account: %r", acc)
        # XXX
        #crypto_util.validate_key_csr(acc.key)
    else:
        acc, acme = None, None

    return client.Client(config, acc, authenticator, installer, acme=acme)


def install(config, plugins):
    """Install a previously obtained cert in a server."""
    # XXX: Update for renewer/RenewableCert
    # FIXME: be consistent about whether errors are raised or returned from
    # this function ...

    try:
        installer, _ = choose_configurator_plugins(config, plugins, "install")
    except errors.PluginSelectionError as e:
        return e.message

    domains = _find_domains(config, installer)
    le_client = _init_le_client(config, authenticator=None, installer=installer)
    assert config.cert_path is not None  # required=True in the subparser
    le_client.deploy_certificate(
        domains, config.key_path, config.cert_path, config.chain_path,
        config.fullchain_path)
    le_client.enhance_config(domains, config)


def plugins_cmd(config, plugins):  # TODO: Use IDisplay rather than print
    """List server software plugins."""
    logger.debug("Expected interfaces: %s", config.ifaces)

    ifaces = [] if config.ifaces is None else config.ifaces
    filtered = plugins.visible().ifaces(ifaces)
    logger.debug("Filtered plugins: %r", filtered)

    if not config.init and not config.prepare:
        print(str(filtered))
        return

    filtered.init(config)
    verified = filtered.verify(ifaces)
    logger.debug("Verified plugins: %r", verified)

    if not config.prepare:
        print(str(verified))
        return

    verified.prepare()
    available = verified.available()
    logger.debug("Prepared plugins: %s", available)
    print(str(available))


def rollback(config, plugins):
    """Rollback server configuration changes made during install."""
    client.rollback(config.installer, config.checkpoints, config, plugins)


def config_changes(config, unused_plugins):
    """Show changes made to server config during installation

    View checkpoints and associated configuration changes.

    """
    client.view_config_changes(config)


def revoke(config, unused_plugins):  # TODO: coop with renewal config
    """Revoke a previously obtained certificate."""
    # For user-agent construction
    config.namespace.installer = config.namespace.authenticator = "none"
    if config.key_path is not None:  # revocation by cert key
        logger.debug("Revoking %s using cert key %s",
                     config.cert_path[0], config.key_path[0])
        key = jose.JWK.load(config.key_path[1])
    else:  # revocation by account key
        logger.debug("Revoking %s using Account Key", config.cert_path[0])
        acc, _ = _determine_account(config)
        key = acc.key
    acme = client.acme_from_config_key(config, key)
    cert = crypto_util.pyopenssl_load_certificate(config.cert_path[1])[0]
    acme.revoke(jose.ComparableX509(cert))


def run(config, plugins):  # pylint: disable=too-many-branches,too-many-locals
    """Obtain a certificate and install."""
    # TODO: Make run as close to auth + install as possible
    # Possible difficulties: config.csr was hacked into auth
    try:
        installer, authenticator = choose_configurator_plugins(config, plugins, "run")
    except errors.PluginSelectionError as e:
        return e.message

    domains = _find_domains(config, installer)

    # TODO: Handle errors from _init_le_client?
    le_client = _init_le_client(config, authenticator, installer)

    lineage, action = _auth_from_domains(le_client, config, domains)

    le_client.deploy_certificate(
        domains, lineage.privkey, lineage.cert,
        lineage.chain, lineage.fullchain)

    le_client.enhance_config(domains, config)

    if len(lineage.available_versions("cert")) == 1:
        display_ops.success_installation(domains)
    else:
        display_ops.success_renewal(domains, action)

    _suggest_donation_if_appropriate(config, action)


def obtain_cert(config, plugins, lineage=None):
    """Implements "certonly": authenticate & obtain cert, but do not install it."""
    # pylint: disable=too-many-locals
    try:
        # installers are used in auth mode to determine domain names
        installer, authenticator = choose_configurator_plugins(config, plugins, "certonly")
    except errors.PluginSelectionError as e:
        logger.info("Could not choose appropriate plugin: %s", e)
        raise

    # TODO: Handle errors from _init_le_client?
    le_client = _init_le_client(config, authenticator, installer)

    action = "newcert"
    # This is a special case; cert and chain are simply saved
    if config.csr is not None:
        assert lineage is None, "Did not expect a CSR with a RenewableCert"
        csr, typ = config.actual_csr
        certr, chain = le_client.obtain_certificate_from_csr(config.domains, csr, typ)
        if config.dry_run:
            logger.info(
                "Dry run: skipping saving certificate to %s", config.cert_path)
        else:
            cert_path, _, cert_fullchain = le_client.save_certificate(
                certr, chain, config.cert_path, config.chain_path, config.fullchain_path)
            _report_new_cert(cert_path, cert_fullchain)
    else:
        domains = _find_domains(config, installer)
        _, action = _auth_from_domains(le_client, config, domains, lineage)

    if config.dry_run:
        _report_successful_dry_run(config)
    elif config.verb == "renew":
        if installer is None:
            # Tell the user that the server was not restarted.
            print("new certificate deployed without reload, fullchain is",
                  lineage.fullchain)
        else:
            # In case of a renewal, reload server to pick up new certificate.
            # In principle we could have a configuration option to inhibit this
            # from happening.
            installer.restart()
            print("new certificate deployed with reload of",
                  config.installer, "server; fullchain is", lineage.fullchain)
    _suggest_donation_if_appropriate(config, action)


def setup_log_file_handler(config, logfile, fmt):
    """Setup file debug logging."""
    log_file_path = os.path.join(config.logs_dir, logfile)
    handler = logging.handlers.RotatingFileHandler(
        log_file_path, maxBytes=2 ** 20, backupCount=10)
    # rotate on each invocation, rollover only possible when maxBytes
    # is nonzero and backupCount is nonzero, so we set maxBytes as big
    # as possible not to overrun in single CLI invocation (1MB).
    handler.doRollover()  # TODO: creates empty letsencrypt.log.1 file
    handler.setLevel(logging.DEBUG)
    handler_formatter = logging.Formatter(fmt=fmt)
    handler_formatter.converter = time.gmtime  # don't use localtime
    handler.setFormatter(handler_formatter)
    return handler, log_file_path


def _cli_log_handler(config, level, fmt):
    if config.text_mode or config.noninteractive_mode or config.verb == "renew":
        handler = colored_logging.StreamHandler()
        handler.setFormatter(logging.Formatter(fmt))
    else:
        handler = log.DialogHandler()
        # dialog box is small, display as less as possible
        handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(level)
    return handler


def setup_logging(config, cli_handler_factory, logfile):
    """Setup logging."""
    fmt = "%(asctime)s:%(levelname)s:%(name)s:%(message)s"
    level = -config.verbose_count * 10
    file_handler, log_file_path = setup_log_file_handler(
        config, logfile=logfile, fmt=fmt)
    cli_handler = cli_handler_factory(config, level, fmt)

    # TODO: use fileConfig?

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # send all records to handlers
    root_logger.addHandler(cli_handler)
    root_logger.addHandler(file_handler)

    logger.debug("Root logging level set at %d", level)
    logger.info("Saving debug log to %s", log_file_path)


def _handle_exception(exc_type, exc_value, trace, config):
    """Logs exceptions and reports them to the user.

    Config is used to determine how to display exceptions to the user. In
    general, if config.debug is True, then the full exception and traceback is
    shown to the user, otherwise it is suppressed. If config itself is None,
    then the traceback and exception is attempted to be written to a logfile.
    If this is successful, the traceback is suppressed, otherwise it is shown
    to the user. sys.exit is always called with a nonzero status.

    """
    logger.debug(
        "Exiting abnormally:%s%s",
        os.linesep,
        "".join(traceback.format_exception(exc_type, exc_value, trace)))

    if issubclass(exc_type, Exception) and (config is None or not config.debug):
        if config is None:
            logfile = "letsencrypt.log"
            try:
                with open(logfile, "w") as logfd:
                    traceback.print_exception(
                        exc_type, exc_value, trace, file=logfd)
            except:  # pylint: disable=bare-except
                sys.exit("".join(
                    traceback.format_exception(exc_type, exc_value, trace)))

        if issubclass(exc_type, errors.Error):
            sys.exit(exc_value)
        else:
            # Here we're passing a client or ACME error out to the client at the shell
            # Tell the user a bit about what happened, without overwhelming
            # them with a full traceback
            err = traceback.format_exception_only(exc_type, exc_value)[0]
            # Typical error from the ACME module:
            # acme.messages.Error: urn:acme:error:malformed :: The request message was
            # malformed :: Error creating new registration :: Validation of contact
            # mailto:none@longrandomstring.biz failed: Server failure at resolver
            if (("urn:acme" in err and ":: " in err and
                 config.verbose_count <= cli.flag_default("verbose_count"))):
                # prune ACME error code, we have a human description
                _code, _sep, err = err.partition(":: ")
            msg = "An unexpected error occurred:\n" + err + "Please see the "
            if config is None:
                msg += "logfile '{0}' for more details.".format(logfile)
            else:
                msg += "logfiles in {0} for more details.".format(config.logs_dir)
            sys.exit(msg)
    else:
        sys.exit("".join(
            traceback.format_exception(exc_type, exc_value, trace)))


def main(cli_args=sys.argv[1:]):
    """Command line argument parsing and main script execution."""
    sys.excepthook = functools.partial(_handle_exception, config=None)
    plugins = plugins_disco.PluginsRegistry.find_all()

    # note: arg parser internally handles --help (and exits afterwards)
    args = cli.prepare_and_parse_args(plugins, cli_args)
    config = configuration.NamespaceConfig(args)
    zope.component.provideUtility(config)

    # Setup logging ASAP, otherwise "No handlers could be found for
    # logger ..." TODO: this should be done before plugins discovery
    for directory in config.config_dir, config.work_dir:
        le_util.make_or_verify_dir(
            directory, constants.CONFIG_DIRS_MODE, os.geteuid(),
            "--strict-permissions" in cli_args)
    # TODO: logs might contain sensitive data such as contents of the
    # private key! #525
    le_util.make_or_verify_dir(
        config.logs_dir, 0o700, os.geteuid(), "--strict-permissions" in cli_args)
    setup_logging(config, _cli_log_handler, logfile='letsencrypt.log')

    logger.debug("letsencrypt version: %s", letsencrypt.__version__)
    # do not log `config`, as it contains sensitive data (e.g. revoke --key)!
    logger.debug("Arguments: %r", cli_args)
    logger.debug("Discovered plugins: %r", plugins)

    sys.excepthook = functools.partial(_handle_exception, config=config)

    # Displayer
    if config.noninteractive_mode:
        displayer = display_util.NoninteractiveDisplay(sys.stdout)
    elif config.text_mode:
        displayer = display_util.FileDisplay(sys.stdout)
    elif config.verb == "renew":
        config.noninteractive_mode = True
        displayer = display_util.NoninteractiveDisplay(sys.stdout)
    else:
        displayer = display_util.NcursesDisplay()
    zope.component.provideUtility(displayer)

    # Reporter
    report = reporter.Reporter()
    zope.component.provideUtility(report)
    atexit.register(report.atexit_print_messages)

    return config.func(config, plugins)


# Maps verbs/subcommands to the functions that implement them
# In principle this should live in cli.HelpfulArgumentParser, but
# due to issues with import cycles and testing, it lives here
VERBS = {"auth": obtain_cert, "certonly": obtain_cert,
         "config_changes": config_changes, "everything": run,
         "install": install, "plugins": plugins_cmd, "renew": cli.renew,
         "revoke": revoke, "rollback": rollback, "run": run}


if __name__ == "__main__":
    err_string = main()
    if err_string:
        logger.warn("Exiting with message %s", err_string)
    sys.exit(err_string)  # pragma: no cover
