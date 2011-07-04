"""A simple guestbook app to test parts of NDB end-to-end."""

import cgi
import logging
import re
import sys
import time

from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.datastore import entity_pb
from webapp2 import webapp2
#from google.appengine.ext import webapp
from google.appengine.ext.webapp import util

from google.appengine.datastore import datastore_query
from google.appengine.datastore import datastore_rpc

from ndb import context
from ndb import eventloop
from ndb import model
from ndb import tasklets

HOME_PAGE = """
<script>
function focus() {
  textarea = document.getElementById('body');
  textarea.focus();
}
</script>
<body onload=focus()>
  Nickname: <a href="/account">%(nickname)s</a> |
  <a href="%(login)s">login</a> |
  <a href="%(logout)s">logout</a>

  <form method=POST action=/>
    <!-- TODO: XSRF protection -->
    <input type=text id=body name=body size=60>
    <input type=submit>
  </form>
</body>
"""

ACCOUNT_PAGE = """
<body>
  Nickname: <a href="/account">%(nickname)s</a> |
  <a href="%(logout)s">logout</a>

  <form method=POST action=/account>
    <!-- TODO: XSRF protection -->
    Email: %(email)s<br>
    New nickname:
    <input type=text name=nickname size=20 value=%(proposed_nickname)s><br>
    <input type=submit name=%(action)s value="%(action)s Account">
    <input type=submit name=delete value="Delete Account">
    <a href=/>back to home page</a>
  </form>
</body>
"""

class Concept(model.Model): 
    """Concept"""
    id = model.IntegerProperty()
    effective_time = model.DateTimeProperty()# server_default="CURRENT_TIMESTAMP")
    active = model.BooleanProperty()# default=True)
    module_id = model.IntegerProperty()#default='900000000001234567')
    definition_status_id = model.IntegerProperty()# default=900000000000130009)
    
    
class Description(model.Model): 
    id = model.IntegerProperty()
    effective_time = model.DateTimeProperty() #server_default="CURRENT_TIMESTAMP")
    active = model.BooleanProperty()# default=True)
    module_id = model.IntegerProperty()# default='900000000001234567')
    concept_id = model.IntegerProperty()# can't be null
    language_code = model.StringProperty()#length=8 ISO-639-1 code.
    type_id = model.IntegerProperty()#default=S.DESCRIPTION.SYNONYM)
    term = model.StringProperty()
    case_significance_id = model.IntegerProperty()# default=None)
    #full_concept = relationship('Concept', backref='descriptions', primaryjoin="Description.concept_id==Concept.id")

class Relationship(model.Model): 
    id = model.IntegerProperty()
    effective_time = model.DateTimeProperty() # server_default="CURRENT_TIMESTAMP")
    active = model.BooleanProperty()# default=True)
    module_id = model.IntegerProperty()# default='900000000001234567')
    source_id = model.IntegerProperty()# default=None)
    destination_id = model.IntegerProperty()# default=None)
    group = model.IntegerProperty()# default=0)
    type_id = model.IntegerProperty()# default='116680003')
    modifier_id = model.IntegerProperty()# default=None)
    characteristic_type_id = model.IntegerProperty()# default='0')
    
class RefSet(model.Model):
    id = model.StringProperty()#uuid
    effective_time = model.DateTimeProperty() # server_default="CURRENT_TIMESTAMP")
    active = model.BooleanProperty()# default=True)
    module_id = model.IntegerProperty()# default='900000000001234567')
    ref_set_id = model.IntegerProperty()# default=None)
    referenced_component_id = model.IntegerProperty()# default=None)
    data =model.BlobProperty()#
    
    
class Account(model.Model):
  """User account."""
  email = model.StringProperty()
  userid = model.StringProperty()
  nickname = model.StringProperty()


class Message(model.Model):
  """Guestbook message."""

  body = model.StringProperty()
  when = model.FloatProperty()
  userid = model.StringProperty()


class UrlSummary(model.Model):
  """Metadata about a URL."""

  MAX_AGE = 60

  url = model.StringProperty()
  title = model.StringProperty()
  when = model.FloatProperty()


def account_key(userid):
  return model.Key(flat=['Account', userid])


def get_account(userid):
  """Return a Future for an Account."""
  return account_key(userid).get_async()


@tasklets.tasklet
def get_nickname(userid):
  """Return a Future for a nickname from an account."""
  account = yield get_account(userid)
  if not account:
    nickname = 'Unregistered'
  else:
    nickname = account.nickname or account.email
  raise tasklets.Return(nickname)


class HomePage(webapp2.RequestHandler):

  @context.toplevel
  def get(self):
    nickname = 'Anonymous'
    user = users.get_current_user()
    if user is not None:
      nickname = yield get_nickname(user.user_id())
    values = {'nickname': nickname,
              'login': users.create_login_url('/'),
              'logout': users.create_logout_url('/'),
              }
    self.response.out.write(HOME_PAGE % values)
    qry, options = self._make_query()
    pairs = yield qry.map_async(self._hp_callback, options=options)
    for key, text in pairs:
      self.response.out.write(text)

  def _make_query(self):
    qry = Message.query().order(-Message.when)
    options = datastore_query.QueryOptions(batch_size=13, limit=43)
    return qry, options

  @tasklets.tasklet
  def _hp_callback(self, message):
    nickname = 'Anonymous'
    if message.userid:
      nickname = yield get_nickname(message.userid)
    # Check if there's an URL.
    body = message.body
    m = re.search(r'(?i)\bhttps?://\S+[^\s.,;\]\}\)]', body)
    if not m:
      escbody = cgi.escape(body)
    else:
      url = m.group()
      pre = body[:m.start()]
      post = body[m.end():]
      title = ''
      key = model.Key(flat=[UrlSummary.GetKind(), url])
      summary = yield key.get_async()
      if not summary or summary.when < time.time() - UrlSummary.MAX_AGE:
        rpc = urlfetch.create_rpc(deadline=0.5)
        urlfetch.make_fetch_call(rpc, url,allow_truncated=True)
        t0 = time.time()
        result = yield rpc
        t1 = time.time()
        logging.warning('url=%r, status=%r, dt=%.3f',
                        url, result.status_code, t1-t0)
        if result.status_code == 200:
          bodytext = result.content
          m = re.search(r'(?i)<title>([^<]+)</title>', bodytext)
          if m:
            title = m.group(1).strip()
          summary = UrlSummary(key=key, url=url, title=title,
                               when=time.time())
          yield summary.put_async()
      hover = ''
      if summary.title:
        hover = ' title="%s"' % summary.title
      escbody = (cgi.escape(pre) +
                 '<a%s href="%s">' % (hover, cgi.escape(url)) +
                 cgi.escape(url) + '</a>' + cgi.escape(post))
    text = '%s - %s - %s<br>' % (cgi.escape(nickname),
                                 time.ctime(message.when),
                                 escbody)
    raise tasklets.Return((-message.when, text))

  @context.toplevel
  def post(self):
    # TODO: XSRF protection.
    body = self.request.get('body', '').strip()
    if body:
      userid = None
      user = users.get_current_user()
      if user:
        userid = user.user_id()
      message = Message(body=body, when=time.time(), userid=userid)
      yield message.put_async()
    self.redirect('/')


class AccountPage(webapp2.RequestHandler):

  @context.toplevel
  def get(self):
    user = users.get_current_user()
    if not user:
      self.redirect(users.create_login_url('/account'))
      return
    email = user.email()
    action = 'Create'
    account, nickname = yield (get_account(user.user_id()),
                               get_nickname(user.user_id()))
    if account is not None:
      action = 'Update'
    if account:
      proposed_nickname = account.nickname or account.email
    else:
      proposed_nickname = email
    values = {'email': email,
              'nickname': nickname,
              'proposed_nickname': proposed_nickname,
              'login': users.create_login_url('/'),
              'logout': users.create_logout_url('/'),
              'action': action,
              }
    self.response.out.write(ACCOUNT_PAGE % values)

  @context.toplevel
  def post(self):
    # TODO: XSRF protection.
    @tasklets.tasklet
    def helper():
      user = users.get_current_user()
      if not user:
        self.redirect(users.create_login_url('/account'))
        return
      account = yield get_account(user.user_id())
      if self.request.get('delete'):
        if account:
          yield account.key.delete_async()
        self.redirect('/account')
        return
      if not account:
        account = Account(key=account_key(user.user_id()),
                          email=user.email(), userid=user.user_id())
      nickname = self.request.get('nickname')
      if nickname:
        account.nickname = nickname
      yield account.put_async()
      self.redirect('/account')
    yield model.transaction_async(helper)



urls = [
  ('/', HomePage),
  ('/account', AccountPage),
  ]

app = webapp2.WSGIApplication(urls)


def main():
  util.run_wsgi_app(app)


if __name__ == '__main__':
  main()

#
#
#class HomeHandler(webapp2.RequestHandler):
#    def get(self, **kwargs):
#        html = '<a href="%s">test item</a>' % self.url_for('view', item='test')
#        self.response.out.write(html)
#
#
#class ViewHandler(webapp2.RequestHandler):
#    def get(self, **kwargs):
#        item = kwargs.get('item')
#        self.response.out.write('You are viewing item "%s".' % item)
#
#
#class HandlerWithError(webapp2.RequestHandler):
#    def get(self, **kwargs):
#        raise ValueError('Oops!')
#
#
#def get_redirect_url(handler, **kwargs):
#    return handler.url_for('view', item='i-came-from-a-redirect')
#
#
#app = webapp2.WSGIApplication([
#    # Home sweet home.
#    webapp2.Route('/', HomeHandler, name='home'),
#    # A route with a named variable.
#    webapp2.Route('/view/<item>', ViewHandler, name='view'),
#    # Loads a handler lazily.
#    webapp2.Route('/lazy', 'handlers.LazyHandler', name='lazy'),
#    # Redirects to a given path.
#    webapp2.Route('/redirect-me', webapp2.RedirectHandler, defaults={'url': '/lazy'}),
#    # Redirects to a URL using a callable to get the destination URL.
#    webapp2.Route('/redirect-me2', webapp2.RedirectHandler, defaults={'url': get_redirect_url}),
#    # No exception should pass. If exceptions are not handled, a 500 page is displayed.
#    webapp2.Route('/exception', HandlerWithError),
#])
#
#
#def main():
#    app.run()
#
#
#if __name__ == '__main__':
#    main()
