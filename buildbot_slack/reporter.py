# Based on the gitlab reporter from buildbot

from __future__ import absolute_import, print_function

from urllib.parse import quote_plus as urlquote_plus

from twisted.internet import defer
from twisted.python import log

from buildbot.process.properties import Interpolate
from buildbot.process.properties import Properties
from buildbot.process.results import CANCELLED
from buildbot.process.results import EXCEPTION
from buildbot.process.results import FAILURE
from buildbot.process.results import RETRY
from buildbot.process.results import SKIPPED
from buildbot.process.results import SUCCESS
from buildbot.process.results import WARNINGS
from buildbot.reporters.base import ReporterBase
from buildbot.reporters.generators.build import BuildStartEndStatusGenerator
from buildbot.reporters.message import MessageFormatterRenderable
from buildbot.util import giturlparse
from buildbot.util import httpclientservice

from buildbot.process.results import statusToString
from buildbot.reporters import http, utils
from buildbot.util.logger import Logger

logger = Logger()

STATUS_EMOJIS = {
    "success": ":sunglassses:",
    "warnings": ":meow_wow:",
    "failure": ":skull:",
    "skipped": ":slam:",
    "exception": ":skull:",
    "retry": ":facepalm:",
    "cancelled": ":slam:",
}
STATUS_COLORS = {
    "success": "#36a64f",
    "warnings": "#fc8c03",
    "failure": "#fc0303",
    "skipped": "#fc8c03",
    "exception": "#fc0303",
    "retry": "#fc8c03",
    "cancelled": "#fc8c03",
}
DEFAULT_HOST = "https://hooks.slack.com"  # deprecated


class SlackStatusPush(ReporterBase):
    name = "SlackStatusPush"
    neededDetails = dict(wantProperties=True)

    def checkConfig(
        self, endpoint, channel=None, host_url=None, username=None, verbose=False,
        debug=None, verify=None, generators=None, **kwargs
    ):
        if not isinstance(endpoint, str):
            logger.warning(
                "[SlackStatusPush] endpoint should be a string, got '%s' instead",
                type(endpoint).__name__,
            )
        elif not endpoint.startswith("http"):
            logger.warning(
                '[SlackStatusPush] endpoint should start with "http...", endpoint: %s',
                endpoint,
            )
        if channel and not isinstance(channel, str):
            logger.warning(
                "[SlackStatusPush] channel must be a string, got '%s' instead",
                type(channel).__name__,
            )
        if username and not isinstance(username, str):
            logger.warning(
                "[SlackStatusPush] username must be a string, got '%s' instead",
                type(username).__name__,
            )
        if host_url and not isinstance(host_url, str):  # deprecated
            logger.warning(
                "[SlackStatusPush] host_url must be a string, got '%s' instead",
                type(host_url).__name__,
            )
        elif host_url:
            logger.warning(
                "[SlackStatusPush] argument host_url is deprecated and will be removed in the next release: specify the full url as endpoint"
            )

    @defer.inlineCallbacks
    def reconfigService(
        self,
        endpoint,
        channel=None,
        host_url=None,  # deprecated
        username=None,
        verbose=False,
        debug=None, verify=None, generators=None,
        attachments=True,
        **kwargs
    ):
        self.debug = debug
        self.verify = verify
        self.verbose = verbose

        if generators is None:
            generators = self._create_default_generators()

        yield super().reconfigService(generators=generators, **kwargs)
        #yield super().reconfigService(**kwargs)

        self.baseUrl = host_url and host_url.rstrip("/")  # deprecated
        if host_url:
            logger.warning(
                "[SlackStatusPush] argument host_url is deprecated and will be removed in the next release: specify the full url as endpoint"
            )
        self.endpoint = endpoint
        self.channel = channel
        self.username = username
        self.attachments = attachments
        self._http = yield httpclientservice.HTTPClientService.getService(
            self.master,
            self.baseUrl or self.endpoint,
            debug=self.debug,
            verify=self.verify,
        )
        self.verbose = verbose
        self.project_ids = {}

    def _create_default_generators(self):
        start_formatter = MessageFormatterRenderable('Build started.')
        end_formatter = MessageFormatterRenderable('Build done.')

        return [
            BuildStartEndStatusGenerator(start_formatter=start_formatter,
                                         end_formatter=end_formatter)
        ]

    @defer.inlineCallbacks
    def getAttachments(self, build):
        sourcestamps = build["buildset"]["sourcestamps"]
        attachments = []

        for sourcestamp in sourcestamps:
            sha = sourcestamp["revision"]

            title = "Build #{buildid}".format(buildid=build["buildid"])
            project = sourcestamp["project"]
            if project:
                title += " for {project} {sha}".format(project=project, sha=sha)
            sub_build = bool(build["buildset"]["parent_buildid"])
            if sub_build:
                title += " {relationship}: #{parent_build_id}".format(
                    relationship=build["buildset"]["parent_relationship"],
                    parent_build_id=build["buildset"]["parent_buildid"],
                )

            fields = []
            if not sub_build:
                branch_name = sourcestamp["branch"]
                if branch_name:
                    fields.append(
                        {"title": "Branch", "value": branch_name, "short": True}
                    )
                repositories = sourcestamp["repository"]
                if repositories:
                    fields.append(
                        {"title": "Repository", "value": repositories, "short": True}
                    )
                responsible_users = yield utils.getResponsibleUsersForBuild(
                    self.master, build["buildid"]
                )
                if responsible_users:
                    fields.append(
                        {
                            "title": "Commiters",
                            "value": ", ".join(responsible_users),
                            "short": True,
                        }
                    )
            attachments.append(
                {
                    "title": title,
                    "title_link": build["url"],
                    "fallback": "{}: <{}>".format(title, build["url"]),
                    "text": "Status: *{status}*".format(
                        status=statusToString(build["results"])
                    ),
                    "color": STATUS_COLORS.get(statusToString(build["results"]), ""),
                    "mrkdwn_in": ["text", "title", "fallback"],
                    "fields": fields,
                }
            )
        return attachments

    @defer.inlineCallbacks
    def getBuildDetailsAndSendMessage(self, build):
        yield utils.getDetailsForBuild(self.master, build, **self.neededDetails)
        text = yield self.getMessage(build)
        postData = {}
        if self.attachments:
            attachments = yield self.getAttachments(build)
            if attachments:
                postData["attachments"] = attachments
        else:
            text += " here: " + build["url"]
        postData["text"] = text

        if self.channel:
            postData["channel"] = self.channel

        postData["icon_emoji"] = STATUS_EMOJIS.get(
            statusToString(build["results"]), ":facepalm:"
        )
        extra_params = yield self.getExtraParams(build)
        postData.update(extra_params)
        return postData

    def getMessage(self, build):
        event_messages = {
            False: "Buildbot started build %s" % build["builder"]["name"],
            True: "Buildbot finished build %s with result: %s"
            % (build["builder"]["name"], statusToString(build["results"])),
        }
        return event_messages.get(build['complete'], "")

    # returns a Deferred that returns None
    def buildStarted(self, reports):
        return self.sendMessage(reports)

    # returns a Deferred that returns None
    def buildFinished(self, reports):
        return self.sendMessage(reports)

    def getExtraParams(self, build):
        return {}

    @defer.inlineCallbacks
    def sendMessage(self, reports):
        report = reports[0]
        build = report['builds'][0]
        postData = yield self.getBuildDetailsAndSendMessage(build)
        if not postData:
            return

        sourcestamps = build["buildset"]["sourcestamps"]

        for sourcestamp in sourcestamps:
            sha = sourcestamp["revision"]
            if sha is None:
                logger.info("no special revision for this")

            logger.info("posting to {url}", url=self.endpoint)
            try:
                if self.baseUrl:
                    # deprecated
                    response = yield self._http.post(self.endpoint, json=postData)
                else:
                    response = yield self._http.post("", json=postData)
                if response.code != 200:
                    content = yield response.content()
                    logger.error(
                        "{code}: unable to upload status: {content}",
                        code=response.code,
                        content=content,
                    )
            except Exception as e:
                logger.error(
                    "Failed to send status for {repo} at {sha}: {error}",
                    repo=sourcestamp["repository"],
                    sha=sha,
                    error=e,
                )
