import re
import pkg_resources
import os.path

from urllib import unquote_plus

from webob import Request

from lxml import etree, html

from repoze.xmliter.serializer import XMLSerializer
from repoze.xmliter.utils import getHTMLSerializer

from diazo.compiler import compile_theme
from diazo.utils import pkg_parse
from diazo.utils import quote_param

DIAZO_OFF_HEADER = 'X-Diazo-Off'

def asbool(value):
    if isinstance(value, basestring):
        value = value.strip().lower()
        if value in ('true', 'yes', 'on', 'y', 't', '1',):
            return True
        elif value in ('false', 'no', 'off', 'n', 'f', '0'):
            return False
        else:
            raise ValueError("String is not true/false: %r" % value)
    else:
        return bool(value)

class FilesystemResolver(etree.Resolver):
    """Resolver for filesystem paths
    """
    def resolve(self, system_url, public_id, context):
        if not '://' in system_url and os.path.exists(system_url):
            return self.resolve_filename(system_url, context)
        else:
            return None

class NetworkResolver(etree.Resolver):
    """Resolver for network urls
    """
    def resolve(self, system_url, public_id, context):
        if '://' in system_url and system_url != 'file:///__diazo__':
            return self.resolve_filename(system_url, context)
        else:
            return None

class PythonResolver(etree.Resolver):
    """Resolver for python:// urls
    """
    
    def resolve(self, system_url, public_id, context):
        if not system_url.lower().startswith('python://'):
            return None
        
        spec = system_url[9:]
        package, resource_name = spec.split('/', 1)
        filename = pkg_resources.resource_filename(package, resource_name)
        
        return self.resolve_filename(filename, context)

class WSGIResolver(etree.Resolver):
    """Resolver that performs a WSGI subrequest
    """
    
    def __init__(self, app):
        self.app = app
    
    def resolve(self, system_url, public_id, context):
        # Ignore URLs with a scheme
        if '://' in system_url:
            return None
        
        # Ignore the special 'diazo:' resolvers
        if system_url.startswith('diazo:'):
            return None
        
        subrequest = Request.blank(system_url)
        response = subrequest.get_response(self.app)
        
        status_code = response.status.split()[0]
        if not status_code == '200':
            return None
        
        return self.resolve_string(response.body, context)

class XSLTMiddleware(object):
    """Apply XSLT in middleware
    """
    
    def __init__(self, app, global_conf,
                 filename=None, tree=None,
                 read_network=False,
                 read_file=True,
                 update_content_length=True,
                 ignored_extensions=(
                     'js', 'css', 'gif', 'jpg', 'jpeg', 'pdf', 'ps', 'doc',
                     'png', 'ico', 'mov', 'mpg', 'mpeg', 'mp3', 'm4a', 'txt',
                     'rtf', 'swf', 'wav', 'zip', 'wmv', 'ppt', 'gz', 'tgz',
                     'jar', 'xls', 'bmp', 'tif', 'tga', 'hqx', 'avi',
                    ),
                 environ_param_map=None,
                 unquoted_params=None,
                 doctype=None,
                 content_type=None,
                 **params
    ):
        """Initialise, giving a filename or parsed XSLT tree.
        
        The parameters are:
        
        * ``filename``, a filename from which to read the XSLT file
        * ``tree``, a pre-parsed lxml tree representing the XSLT file
        
        ``filename`` and ``tree`` are mutually exclusive.
        
        * ``read_network``, should be set to True to allow resolving resources
          from the network.
        * ``read_file``, should be set to False to disallow resolving resources
          from the filesystem.
        * ``update_content_length``, can be set to False to avoid calculating
          an updated Content-Length header when applying the transformation.
          This is only a good idea if some middleware higher up the chain
          is going to set the content length instead.
        * ``ignored_extensions`` can be set to a list of filename extensions
          for which the transformation should never be applied
        * ``environ_param_map`` can be set to a dict of environ keys to
          parameter names. The corresponding values will then be sent to the
          transformation as parameters.
        * ``unquoted_params``, can be set to a list of parameter names which
          will not be quoted.
        * ``doctype``, can be set to a string which will replace that set in
          the XSLT, for example, "<!DOCTYPE html>".
        * ``content_type``, can be set to a string which will be set in the
          Content-Type header. By default it is inferred from the stylesheet.
         
        Additional keyword arguments will be passed to the transformation as
        parameters.
        """
        
        self.app = app
        self.global_conf = global_conf
        
        if filename is not None:
            xslt_file = open(filename)
            source = xslt_file.read()
            tree = etree.fromstring(source)
            xslt_file.close()
        
        if content_type is None:
            mediatype = tree.xpath('/xsl:stylesheet/xsl:output/@media-type',
                                   namespaces=dict(xsl="http://www.w3.org/1999/XSL/Transform"))
            if mediatype:
                content_type = mediatype[-1]
            else:
                method = tree.xpath('/xsl:stylesheet/xsl:output/@method',
                                    namespaces=dict(xsl="http://www.w3.org/1999/XSL/Transform"))
                if method:
                    method = method[-1]
                    if method.lower() == 'html':
                        content_type = 'text/html'
                    elif method.lower() == 'text':
                        content_type = 'text/plain'
                    elif method.lower() == 'xml':
                        content_type = 'text/xml'
        self.content_type = content_type
        
        self.read_network = asbool(read_network)
        self.read_file = asbool(read_file)
        self.access_control = etree.XSLTAccessControl(read_file=self.read_file, write_file=False, create_dir=False, read_network=self.read_network, write_network=False)
        self.transform = etree.XSLT(tree, access_control=self.access_control)
        self.update_content_length = asbool(update_content_length)
        self.ignored_extensions = ignored_extensions
        
        self.ignored_pattern = re.compile("^.*\.(%s)$" % '|'.join(ignored_extensions))
        
        self.environ_param_map = environ_param_map or {}
        if isinstance(unquoted_params, basestring):
            unquoted_params = unquoted_params.split()
        self.unquoted_params = unquoted_params and frozenset(unquoted_params) or ()
        self.params = params
        self.doctype = doctype
    
    def __call__(self, environ, start_response):
        request = Request(environ)
        
        ignore = self.should_ignore(request)

        if not ignore:
            # We do not deal with Range requests
            try:
                del request.headers['Range']
            except KeyError:
                pass

        response = request.get_response(self.app)

        sr = self._sr(start_response)
        app_iter = response(environ, sr)
        
        if ignore or not self.should_transform(response):
            start_response(self._status,
                           self._response_headers,
                           self._exc_info)
            return app_iter
        
        # Set up parameters
        
        params = {}
        for key, value in self.environ_param_map.items():
            if key in environ:
                if value in self.unquoted_params:
                    params[value] = environ[key]
                else:
                    params[value] = quote_param(environ[key])
        for key, value in self.params.items():
            if key in self.unquoted_params:
                params[key] = value
            else:
                params[key] = quote_param(value)
        
        # Apply the transformation
        app_iter = getHTMLSerializer(app_iter)
        tree = self.transform(app_iter.tree, **params)
        
        # Set content type
        # Unfortunately lxml does not expose docinfo.mediaType
        content_type = self.content_type
        if content_type is None:
            if tree.getroot().tag == 'html':
                content_type = 'text/html'
            else:
                content_type = 'text/xml'
        encoding = tree.docinfo.encoding
        if not encoding:
            encoding = "UTF-8"
        response.headers['Content-Type'] = '%s; charset=%s' % (content_type, encoding)
        
        app_iter = XMLSerializer(tree, doctype=self.doctype)
        
        # Calculate the content length - we still return the parsed tree
        # so that other middleware could avoid having to re-parse, even if
        # we take a hit on serialising here
        if self.update_content_length and 'Content-Length' in response.headers:
            response.headers['Content-Length'] = str(len(str(app_iter)))
        
        # Remove Content-Range if set by the application we theme
        if self.update_content_length and 'Content-Range' in response.headers:
            del(response.headers['Content-Range'])

        # Start response here, after we update response headers
        self._response_headers = response.headers.items()
        start_response(self._status,
                       self._response_headers,
                       self._exc_info)
        # Return a repoze.xmliter XMLSerializer, which helps avoid re-parsing
        # the content tree in later middleware stages
        return app_iter

    def _sr(self, start_response):
        """Capture a start_response call
        """
        def callback(status, response_headers, exc_info=None):
            self._status = status
            self._response_headers = response_headers
            self._exc_info = exc_info
        return callback
   
    def should_ignore(self, request):
        """Determine if we should ignore the request
        """
        
        if asbool(request.headers.get(DIAZO_OFF_HEADER, 'no')):
            return True
        
        if request.method == 'HEAD':
            # response will have no content
            return True

        path = request.path_info
        if self.ignored_pattern.search(path) is not None:
            return True
        
        return False
    
    def should_transform(self, response):
        """Determine if we should transform the response
        """
        
        if asbool(response.headers.get(DIAZO_OFF_HEADER, 'no')):
            return False
        
        content_type = response.headers.get('Content-Type')
        if not content_type or not (
            content_type.lower().startswith('text/html') or
            content_type.lower().startswith('application/xhtml+xml')
        ):
            return False
        
        content_encoding = response.headers.get('Content-Encoding')
        if content_encoding in ('zip', 'deflate', 'compress',):
            return False
        
        status_code = response.status.split()[0]
        if status_code.startswith('3') or status_code == '204' or status_code == '401':
            return False
        
        return True

class DiazoMiddleware(object):
    """Invoke the Diazo transform as middleware
    """
    
    def __init__(self, app, global_conf, rules,
                 theme=None,
                 prefix=None,
                 includemode='document',
                 debug=False,
                 read_network=False,
                 read_file=True,
                 update_content_length=True,
                 ignored_extensions=(
                     'js', 'css', 'gif', 'jpg', 'jpeg', 'pdf', 'ps', 'doc',
                     'png', 'ico', 'mov', 'mpg', 'mpeg', 'mp3', 'm4a', 'txt',
                     'rtf', 'swf', 'wav', 'zip', 'wmv', 'ppt', 'gz', 'tgz',
                     'jar', 'xls', 'bmp', 'tif', 'tga', 'hqx', 'avi',
                    ),
                environ_param_map=None,
                unquoted_params=None,
                doctype=None,
                content_type=None,
                filter_xpath=False,
                **params
    ):
        """Create the middleware. The parameters are:
        
        * ``rules``, the rules file
        * ``theme``, a URL to the theme file (may be a file:// URL)
        * ``debug``, set to True to recompile the theme on each request
        * ``prefix`` can be set to a string that will be prefixed to
          any *relative* URL referenced in an image, link or stylesheet in the
          theme HTML file before the theme is passed to the compiler. This
          allows a theme to be written so that it can be opened and views
          standalone on the filesystem, even if at runtime its static
          resources are going to be served from some other location. For
          example, an ``<img src="images/foo.jpg" />`` can be turned into 
          ``<img src="/static/images/foo.jpg" />`` with a ``prefix`` of
          "/static".
        * ``includemode`` can be set to 'document', 'esi' or 'ssi' to change
          the way in which includes are processed
        * ``read_network``, should be set to True to allow resolving resources
          from the network.
        * ``read_file``, should be set to False to disallow resolving resources
          from the filesystem.
        * ``update_content_length``, can be set to False to avoid calculating
          an updated Content-Length header when applying the transformation.
          This is only a good idea if some middleware higher up the chain
          is going to set the content length instead.
        * ``ignored_extensions`` can be set to a list of filename extensions
          for which the transformation should never be applied
        * ``environ_param_map`` can be set to a dict of environ keys to
          parameter names. The corresponding values will then be sent to the
          transformation as parameters.
        * ``unquoted_params``, can be set to a list of parameter names which
          will not be quoted.
        * ``doctype``, can be set to a string which will replace the default
          XHTML 1.0 transitional Doctype or that set in the Diazo theme. For
          example, "<!DOCTYPE html>".
        * ``content_type``, can be set to a string which will be set in the
          Content-Type header. By default it is inferred from the stylesheet.
        * ``filter_xpath``, should be set to True to enable filter_xpath support
          for external includes.
        
        Additional keyword arguments will be passed to the theme
        transformation as parameters.
        """
        
        self.app = app
        self.global_conf = global_conf
        
        self.rules = rules
        self.theme = theme
        self.absolute_prefix = prefix
        self.includemode = includemode
        self.debug = asbool(debug)
        self.read_network = asbool(read_network)
        self.read_file = asbool(read_file)
        self.update_content_length = asbool(update_content_length)
        self.ignored_extensions = ignored_extensions
        self.doctype = doctype
        self.content_type = content_type
        self.unquoted_params = unquoted_params
        self.filter_xpath = asbool(filter_xpath)
        
        self.access_control = etree.XSLTAccessControl(read_file=self.read_file, write_file=False, create_dir=False, read_network=self.read_network, write_network=False)
        self.transform_middleware = None
        self.filter_middleware = self.get_filter_middleware()
        
        self.environ_param_map = environ_param_map or {}
        self.environ_param_map.update({
                'diazo.path': 'path',
                'diazo.host': 'host',
                'diazo.scheme': 'scheme',
            })
        
        self.params = params.copy()
    
    def compile_theme(self):
        """Compile the Diazo theme, returning an lxml tree (containing an XSLT
        document)
        """
        
        filesystem_resolver = FilesystemResolver(self.app)
        wsgi_resolver = WSGIResolver(self.app)
        python_resolver = PythonResolver()
        network_resolver = NetworkResolver()
        
        rules_parser = etree.XMLParser(recover=False)
        rules_parser.resolvers.add(filesystem_resolver)
        rules_parser.resolvers.add(wsgi_resolver)
        rules_parser.resolvers.add(python_resolver)
        if self.read_network:
            rules_parser.resolvers.add(network_resolver)
        
        theme_parser = etree.HTMLParser()
        theme_parser.resolvers.add(filesystem_resolver)
        theme_parser.resolvers.add(wsgi_resolver)
        theme_parser.resolvers.add(python_resolver)
        if self.read_network:
            theme_parser.resolvers.add(network_resolver)
        
        xsl_params = self.params.copy()
        for value in self.environ_param_map.values():
            if value not in xsl_params:
                xsl_params[value] = None
        
        return compile_theme(self.rules,
                theme=self.theme,
                absolute_prefix=self.absolute_prefix,
                includemode=self.includemode,
                access_control=self.access_control,
                read_network=self.read_network,
                parser=theme_parser,
                rules_parser=rules_parser,
                xsl_params=xsl_params,
            )
    
    def get_transform_middleware(self):
        return XSLTMiddleware(self.app, self.global_conf,
                tree=self.compile_theme(),
                read_network=self.read_network,
                read_file=self.read_file,
                update_content_length=self.update_content_length,
                ignored_extensions=self.ignored_extensions,
                environ_param_map=self.environ_param_map,
                doctype=self.doctype,
                content_type=self.content_type,
                unquoted_params=self.unquoted_params,
                **self.params
            )

    def get_filter_middleware(self):
        tree = pkg_parse('filter_xhtml.xsl')
        return XSLTMiddleware(self.app, self.global_conf,
                tree=tree,
                read_network=False,
                read_file=False,
                update_content_length=self.update_content_length,
                ignored_extensions=self.ignored_extensions,
                environ_param_map={'diazo.filter_xpath': 'xpath'},
                doctype='',
                content_type=self.content_type,
                unquoted_params=['xpath'],
            )

    def __call__(self, environ, start_response):
        if self.filter_xpath:
            filter_xpath = ';filter_xpath='
            query_string = environ.get('QUERY_STRING', '')
            if filter_xpath in query_string:
                environ['QUERY_STRING'], xpath = query_string.rsplit(filter_xpath, 1)
                environ['diazo.filter_xpath'] = unquote_plus(xpath)
                return self.filter_middleware(environ, start_response)
        
        transform_middleware = self.transform_middleware
        if transform_middleware is None or self.debug:
            transform_middleware = self.get_transform_middleware()
        if transform_middleware is not None and not self.debug:
            self.transform_middleware = transform_middleware
        
        # Set up variables, some of which are used as transform parameters
        request = Request(environ)
        
        environ['diazo.rules'] = self.rules
        environ['diazo.absolute_prefix'] = self.absolute_prefix
        environ['diazo.path'] = request.path
        environ['diazo.host'] = request.host
        environ['diazo.scheme'] = request.host
            
        return transform_middleware(environ, start_response)
