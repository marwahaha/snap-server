#!/usr/bin/env python2

from __future__ import print_function

try:
    import gevent
    import gevent.monkey
    import gevent.wsgi
    import gevent.fileobject
    gevent.monkey.patch_all()

    def fileProxy(fobj):
        return gevent.fileobject.FileObjectThread(fobj)
except ImportError:

    def fileProxy(fobj):
        return fobj

import sqlalchemy
import sqlalchemy.engine as sqlengine
import sqlalchemy.ext.declarative
from sqlalchemy.orm import relationship, sessionmaker, join
from sqlalchemy import Column, ForeignKey, Integer, String, Table, Boolean
import falcon

import base64
import xml.etree.ElementTree as etree
import xml.dom.minidom as mdom
import re
import traceback
import hashlib
import random
import os
import os.path
import datetime
import smtplib
from email.mime.text import MIMEText
import string
from contextlib import contextmanager
import urllib
import wsgiref.util


@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    session = Session()
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()


HASH_ID_LEN = 40
STORAGE_DIR = 'storage'

Base = sqlalchemy.ext.declarative.declarative_base()

shares = Table(
    'shares', Base.metadata,
    Column('userName', String, ForeignKey('users.userName')),
    Column('projId', String(HASH_ID_LEN), ForeignKey('projects.projId'))
    )


course_teachers = Table(
    'course_teachers', Base.metadata,
    Column('teacher', String, ForeignKey('users.userName')),
    Column('course', Integer, ForeignKey('courses.courseId'))
    )

course_students = Table(
    'course_students', Base.metadata,
    Column('student', String, ForeignKey('users.userName')),
    Column('course', Integer, ForeignKey('courses.courseId'))
    )


course_assignments = Table(
    'course_assignments', Base.metadata,
    Column('course', Integer, ForeignKey('courses.courseId')),
    Column('assignment', Integer, ForeignKey('assignments.assignId'))
    )

assignment_submissions = Table(
    'assignment_submissions', Base.metadata,
    Column('assignment', Integer, ForeignKey('assignments.assignId')),
    Column('submissions', Integer, ForeignKey('submissions.submitId'))
    )

submission_members = Table(
    'submission_members', Base.metadata,
    Column('submissions', String, ForeignKey('submissions.submitId')),
    Column('users', String, ForeignKey('users.userName'))
    )

project_owners = Table(
    'project_owners', Base.metadata,
    Column('project', String, ForeignKey('projects.projId')),
    Column('users', String, ForeignKey('users.userName'))
    )

teacher_shares = Table(
    'teacher_shares', Base.metadata,
    Column('course', String, ForeignKey('courses.courseId')),
    Column('project', String, ForeignKey('projects.projId')),
    )

student_shares = Table(
    'student_shares', Base.metadata,
    Column('course', String, ForeignKey('courses.courseId')),
    Column('project', String, ForeignKey('projects.projId')),
    )


class User(Base):
    __tablename__ = 'users'

    userName = Column(String, primary_key=True)
    password = Column(String)
    email = Column(String)
    projects = relationship('Project', secondary=shares)
    coursesTeaching = relationship('Course', secondary=course_teachers)
    coursesTaking = relationship('Course', secondary=course_students)

    def toXMLName(self):
        return Elt('user', {'userName': self.userName})

    @staticmethod
    def fromRequest(session, req):
        userName = forceParam(req, 'userName')
        user = session.query(User) \
                      .filter(User.userName == userName) \
                      .first()
        if user is None:
            raise NoSuchUser()
        return user


class Revision(Base):
    __tablename__ = 'revisions'

    revId = Column(String(HASH_ID_LEN), primary_key=True)
    prevId = Column(String(HASH_ID_LEN),
                    ForeignKey('revisions.revId'))
    prev = relationship('Revision')

    def filename(self):
        return os.path.join(STORAGE_DIR, self.revId + '.revision')

    def save(self, contents):
        f = fileProxy(open(self.filename(), 'w'))
        f.write(contents)

    def load(self):
        f = fileProxy(open(self.filename()))
        return f.read()

    @staticmethod
    def fromRequest(session, req):
        revId = forceParam(req, 'revId')
        rev = session.query(Revision) \
                     .filter(Revision.revId == revId) \
                     .first()
        if rev is None:
            raise NoSuchRevision()
        return rev

    def toXML(self):
        el = Elt('revision', {'revId': self.revId})
        el.appendChild(Elt('prevId', text=self.prevId))
        data = Elt('data')
        data.append(mdom.parseString(self.load()).firstChild)
        el.appendChild(data)
        return el


class Elt(mdom.Element):

    def __init__(self, tag, attrib=None, text='', children=()):
        mdom.Element.__init__(self, tag)
        if attrib is not None:
            for k, v in attrib.items():
                if None not in (k, v):
                    self.setAttribute(k, v)
        if text:
            self.appendChild(mdom.Text())
            self.firstChild.replaceWholeText(text)
        for child in children:
            self.appendChild(child)

    def append(self, child):
        self.appendChild(child)
        return self


def formatXML(elt):
    return elt.toprettyxml()


class Project(Base):
    __tablename__ = 'projects'

    projId = Column(String(HASH_ID_LEN), primary_key=True)
    headId = Column(String(HASH_ID_LEN), ForeignKey('revisions.revId'))
    sharedName = Column(String)

    members = relationship('User', secondary=shares)
    owners = relationship('User', secondary=project_owners)
    course_shared_with_teachers = relationship('Course',
                                               secondary=teacher_shares)
    course_shared_with_students = relationship('Course',
                                               secondary=student_shares)
    head = relationship('Revision')
    public = Column(Boolean)

    def getURI(self, req):
        env = dict(req.env)
        env['PATH_INFO'] = '/GetRevision'
        env['QUERY_STRING'] = urllib.urlencode({'revId': self.headId})
        return wsgiref.util.request_uri(env)

    def toXML(self, req):
        proj = Elt('project')
        proj.appendChild(Elt('projId', text=self.projId))
        for owner in self.owners:
            proj.appendChild(Elt('owner').append(owner.toXMLName()))
        for mem in self.members:
            proj.appendChild(Elt('member').append(mem.toXMLName()))
        if self.head is not None:
            proj.appendChild(Elt('URI', text=self.getURI(req)))
        if self.sharedName is not None:
            proj.appendChild(Elt('sharedName', text=self.sharedName))
        return proj

    def canRead(self, user):
        if user in self.members:
            return True
        if user in self.owners:
            return True
        for course in self.course_shared_with_teachers:
            if user in course.teachers:
                return True
        for course in self.course_shared_with_students:
            if user in course.students or user in course.teachers:
                return True

    @staticmethod
    def fromRequest(session, req):
        projId = forceParam(req, 'projId')
        proj = session.query(Project) \
                      .filter(Project.projId == projId) \
                      .first()
        if proj is None:
            raise NoSuchProject()
        return proj


class Course(Base):
    __tablename__ = 'courses'

    courseId = Column(String(HASH_ID_LEN), primary_key=True)
    teachers = relationship('User', secondary=course_teachers)
    students = relationship('User', secondary=course_students)
    name = Column(String)

    @staticmethod
    def fromRequest(session, req):
        courseId = forceParam(req, 'courseId')
        course = session.query(Course) \
                        .filter(Course.courseId == courseId) \
                        .first()
        if course is None:
            raise NoSuchCourse()
        return course

    def toXMLId(self):
        return Elt('course', {'courseId': self.courseId, 'name': self.name})


class Assignment(Base):
    __tablename__ = 'assignments'

    assignId = Column(String(HASH_ID_LEN), primary_key=True)
    course = relationship('Course', secondary=course_assignments)
    name = Column('name', String)
    submissions = relationship('Submission', secondary=assignment_submissions)

    @staticmethod
    def fromRequest(session, req):
        assignId = forceParam(req, 'assignId')
        assign = session.query(Assignment) \
                        .filter(Assignment.assignId == assignId) \
                        .first()
        if assign is None:
            raise NoSuchAssignment()
        return assign

    def toXMLId(self):
        return Elt('assignment', {'assignId': self.assignId})


class Submission(Base):
    __tablename__ = 'submissions'

    submitId = Column(String(HASH_ID_LEN), primary_key=True)
    assignment = relationship('Assignment', secondary=assignment_submissions)
    revisionId = Column(String(HASH_ID_LEN), ForeignKey('revisions.revId'))
    projectId = Column(String(HASH_ID_LEN), ForeignKey('projects.projId'))
    submitterName = Column(String, ForeignKey('users.userName'))
    revision = relationship('Revision')
    project = relationship('Project')
    submitter = relationship('User')
    members = relationship('User', secondary=submission_members)
    time = Column('time', sqlalchemy.DateTime)

    @staticmethod
    def fromRequest(session, req):
        assignId = forceParam(req, 'assignId')
        assign = session.query(Assignment) \
                        .filter(Assignment.assignId == assignId) \
                        .first()
        if assign is None:
            raise NoSuchAssignment()
        return assign

    def toShortXML(self):
        return Elt('submission', {'submitId': self.submitId,
                                  'revId': self.revision.revId,
                                  'time': self.time})


def split_auth_token(token):
    if ' ' in token:
        token = token.split(' ')[-1]
    decoded = base64.b64decode(token)
    return decoded.split(':')


def getUserPass(req):
    token = req.get_header('Authorization')
    if token:
        return split_auth_token(token)
    else:
        token = req.get_header('Snap-Server-Authorization')
        if token:
            return split_auth_token(token)
        else:
            return (None, None)


def requestLogin(resp):
    resp.status = falcon.HTTP_401
    resp.set_header('WWW-Authenticate', 'Basic realm="SnapServer"')


def forceUserPass(req, resp, params=None):
    username, password = getUserPass(req)
    if None in (username, password):
        requestLogin(resp)
        raise NeedAuthentication()
    else:
        return username, password


def xmlError(msg):
    return formatXML(Elt('error', attrib={'reason': msg}))


def sendError(resp, msg):
    respondXML(resp, falcon.HTTP_500, xmlError(msg))


def handle_exception(exp, req, resp, params):
    respondXML(resp, falcon.HTTP_500, xmlError(traceback.format_exc()))


# Exceptions


class ServerException(Exception):

    @staticmethod
    def handle_callback(exp, req, resp, params):
        return exp.handle(req, resp, params)

    def handle(self, req, resp, params):
        handle_exception(self, req, resp, params)


class NotAuthenticated(ServerException):
    pass


class NotAuthorized(ServerException):

    def handle(self, req, resp, params):
        respondXML(resp, falcon.HTTP_403, xmlError('Not authorized'))


class NotPermitted(ServerException):

    def handle(self, req, resp, params):
        respondXML(resp, falcon.HTTP_400, xmlError('Not permitted'))


class NeedAuthentication(ServerException):

    def handle(self, req, resp, params):
        requestLogin(resp)
        resp.body = xmlError('Need authentication')
        resp.content_type = 'application/xml; charset=utf-8'


class IncorrectPassword(ServerException):

    def handle(self, req, resp, params):
        requestLogin(resp)
        resp.body = xmlError('Incorrect password')
        resp.content_type = 'application/xml; charset=utf-8'


class NoSuchUser(ServerException):

    def handle(self, req, resp, params):
        respondXML(resp, falcon.HTTP_500, xmlError('User does not exist.'))


class NoSuchProject(ServerException):
    pass


class NoSuchCourse(ServerException):
    pass


class NoSuchAssignment(ServerException):
    pass


class NoSuchRevision(ServerException):
    pass


class MissingParameter(ServerException):

    def __init__(self, param):
        self._param = param
        ServerException.__init__(self)

    def handle(self, req, resp, params):
        msg = xmlError('Missing parameter {0}.'.format(self._param))
        respondXML(resp, falcon.HTTP_400, msg)


class UserLogicError(ServerException):

    def __init__(self, msg):
        self._msg = msg
        ServerException.__init__(self)

    def handle(self, req, resp, params):
        respondXML(resp, falcon.HTTP_400, xmlError(self._msg))


class UnknownURL(ServerException):

    def handle(self, req, resp, params):
        respondXML(resp, falcon.HTTP_400, xmlError('Could not parse url.'))


usernameRe = re.compile('[A-z0-9_.-]+')


def validUsername(username):
    return isinstance(username, str) and usernameRe.match(username)


def xmlSuccess(*args, **kwargs):
    return formatXML(Elt('success', *args, **kwargs))


def hash_password(username, password):
    sha1 = hashlib.sha1()
    # Add the username for salting purposes
    sha1.update('SnapServer')
    sha1.update(username)
    sha1.update(password)
    sha1.update(username)
    return sha1.hexdigest()


def userExists(username):
    with session_scope() as session:
        res = session.query(User) \
                     .filter(User.userName == username) \
                     .count() != 0
        return res


def auth(session, req, resp):
    username, password = forceUserPass(req, resp)
    if None in (username, password):
        raise NeedAuthentication()
    users = session.query(User).filter(User.userName == username).all()
    if len(users) == 0:
        raise NoSuchUser()
    user = users[0]
    if hash_password(username, password) != user.password:
        raise IncorrectPassword()
    else:
        return user


def respondXML(resp, status, body):
    resp.content_type = 'application/xml; charset=utf-8'
    resp.status = status
    resp.body = body


def generate_password():
    chars = [random.choice(string.letters + string.digits) for i in range(6)]
    return ''.join(chars)


def send_initial_email(user, password):
    print('Sending initial email!')
    msg = MIMEText('Welcome to Snap! {0} your password has been set to {1}'
                   .format(user.userName, password))
    msg['Subject'] = 'Welcome to Snap!'
    msg['To'] = user.email

    s = smtplib.SMTP('localhost')
    s.sendmail('', [user.email], msg.as_string())
    print('sending email', msg.as_string())
    s.quit()


def send_reset_email(user, password):
    msg = MIMEText('Welcome to Snap! {0} your password has been reset to {1}'
                   .format(user.userName, password))
    msg['Subject'] = 'Welcome to Snap!'
    msg['To'] = user.email

    s = smtplib.SMTP('localhost')
    s.sendmail('', [user.email], msg.as_string())
    print('sending email', msg.as_string())
    s.quit()


def formatHash(hsh):
    return format(hsh, '0{0}x'.format(HASH_ID_LEN))


def generateHashId():
    return formatHash(random.randrange(0, 2**160))


def generateProjId():
    return generateHashId()


def generateCourseId():
    return generateHashId()


def generateAssignmentId():
    return generateHashId()


def generateSubmissionId():
    return generateHashId()


def forceParam(req, paramName):
    param = req.get_param(paramName)
    if param is None:
        raise MissingParameter(paramName)
    else:
        return param


def get_or_create(session, model, defaults=None, *args, **kwargs):
    instance = session.query(model).filter_by(*args, **kwargs).first()
    if instance is not None:
        return instance, False
    else:
        params = dict((k, v) for k, v in kwargs.iteritems() if not
                      isinstance(v, sqlalchemy.sql.ClauseElement))
        if defaults is not None:
            params.update(defaults)
        instance = model(**params)
        session.add(instance)
        return instance, True


# Handlers


class RootHandler(object):

    def on_options(self, req, resp):
        respondXML(resp, falcon.HTTP_204, xmlSuccess())


class AddStudent(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            course = Course.fromRequest(session, req)
            if user not in course.teachers:
                raise NotAuthorized()
            student = User.fromRequest(session, req)
            course.students.append(student)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class AddTeacher(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            userName = forceParam(req, 'userName')
            course = Course.fromRequest(session, req)
            if user not in course.teachers:
                raise NotAuthorized()
            teacher = User.fromRequest(session, req)
            course.teachers.append(teacher)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class ChangePassword(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req)
            new_password = forceParam('newPassword')
            user.password = hash_password(user.userName, new_password)
            session.add(user)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class CreateAssignment(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            course = Course.fromRequest(session, req)
            name = forceParam(req, 'name')
            if user not in course.teachers:
                raise NotAuthorized()
            assignId = generateAssignmentId()
            assignment = Assignment(assignId=assignId, courseId=courseId,
                                    name=name)
            success = Elt('success', {'assignId': assignId})
            respondXML(resp, falcon.HTTP_200, formatXML(success))


class CreateCourse(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            name = req.get_param('name')
            courseId = generateCourseId()
            course = Course(courseId=courseId, name=name, teachers=[user])
            session.add(course)
            el = Elt('success', {'courseId': courseId})
            respondXML(resp, falcon.HTTP_200, formatXML(el))


class CreateProject(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            projId = generateProjId()
            proj = Project(projId=projId, owners=[user])
            proj.members.append(user)
            session.add(proj)
            el = Elt('success', {'projId': projId})
            respondXML(resp, falcon.HTTP_200, formatXML(el))


class CreateUser(RootHandler):

    def on_get(self, req, resp):
        username = req.get_param('userName')
        password = req.get_param('password')
        if username is None:
            username, password = forceUserPass(req, resp)
        email = req.get_param('email')
        send_email = False
        if not validUsername(username):
            return sendError(resp,
                             '{0} is not a valid username.'.format(username))
        if userExists(username):
            return sendError(resp, '{0} is already in use.'.format(username))
        if password is None and email is not None:
            password = generate_password()
            send_email = True
        with session_scope() as session:
            user = User(userName=username,
                        password=hash_password(username, password),
                        email=email)
            session.add(user)
            res = Elt('success')
            res.appendChild(Elt('user', {
                'userName': username,
                'password': password,
                'email': email
                }))
            respondXML(resp, falcon.HTTP_200, formatXML(res))
            if send_email:
                send_initial_email(user, password)


class Enroll(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            course = Course.fromRequest(session, req)
            course.students.append(user)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class GetProjectByName(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = User.fromRequest(session, req)
            projectName = forceParam(req, 'projectName')
            projects = session.query(Project) \
                              .filter(Project.members.contains(user)) \
                              .filter(Project.sharedName == projectName) \
                              .all()
            success = Elt('success')
            for proj in projects:
                success.appendChild(proj.toXML(req))
            respondXML(resp, falcon.HTTP_200, formatXML(success))


class GetRevision(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            revision = Revision.fromRequest(session, req)
            success = Elt('success')
            success.appendChild(revision.toXML())
            respondXML(resp, falcon.HTTP_200, formatXML(success))


class ListAssignments(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            course = Course.fromRequest(session, req)
            assigns = session.query(Assignment) \
                             .filter(Assignment.course == course) \
                             .all()
            success = Elt('success')
            for assign in assigns:
                success.appendChild(assign.toXMLId())
            respondXML(resp, falcon.HTTP_200, formatXML(success))


class ListCoursesEnrolled(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            success = Elt('success')
            for course in user.coursesTaking:
                success.appendChild(course.toXMLId())
            respondXML(resp, falcon.HTTP_200, formatXML(success))


class ListCoursesTeaching(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            teacher = User.fromRequest(session, req)
            success = Elt('success')
            for course in teacher.coursesTeaching:
                success.appendChild(course.toXMLId())
            respondXML(resp, falcon.HTTP_200, formatXML(success))


class ListMembers(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.members:
                raise NotAuthorized()
            success = Elt('success')
            for member in project.members:
                success.appendChild(member.toXMLName())
            respondXML(resp, falcon.HTTP_200, formatXML(success))


class ListProjects(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            projects = session.query(Project) \
                              .filter(Project.members.contains(user)) \
                              .all()
            success = Elt('success')
            for proj in projects:
                success.appendChild(proj.toXML(req))
            respondXML(resp, falcon.HTTP_200, formatXML(success))


class ListStudents(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            course = Course.fromRequest(session, req)
            if user not in course.teachers:
                raise NotAuthorized()
            success = Elt('success')
            for student in course.students:
                success.appendChild(student.toXMLName())
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class ListSubmissions(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            assignment = Assignment.fromRequest(session, req)
            if user not in assignment.course.teachers:
                raise NotAuthorized()
            success = Elt('success')
            for submission in assignment.submissions:
                success.appendChild(submission.toShortXML())
            respondXML(resp, falcon.HTTP_200, formatXML(success))


class ListTeachers(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            course = Course.fromRequest(session, req)
            success = Elt('success')
            for teacher in course.teachers:
                success.appendChild(teacher.toXMLName())
            respondXML(resp, falcon.HTTP_200, formatXML(success))


class LoadProject(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.members:
                raise NotAuthorized()
            success = Elt('success')
            success.appendChild(project.toXML(req))
            respondXML(resp, falcon.HTTP_200, formatXML(success))


class MakePublic(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.members:
                raise NotAuthorized()
            project.public = True
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class RemoveStudent(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            course = Course.fromRequest(session, req)
            if user not in course.teachers:
                raise NotAuthorized()
            student = User.fromRequest(session, req)
            try:
                course.students.remove(student)
            except ValueError:
                raise UserLogicError('User is not taking this course.')
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class RemoveTeacher(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            course = Course.fromRequest(session, req)
            teacher = User.fromRequest(session, req)
            if user not in course.teachers:
                raise NotAuthorized()
            if len(course.teachers) == 1:
                raise NotPermitted()
            try:
                course.teachers.remove(teacher)
            except ValueError:
                raise UserLogicError('User is not teaching this course.')
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class ResetPassword(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = User.fromRequest(session, req)
            if user.email is None:
                raise UserLogicError('Cannot reset password without email.')
            password = generate_password()
            user.password = hash_password(username, password)
            session.add(user)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())
            send_reset_email(user, password)


class SaveProject(RootHandler):

    def on_post(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.members:
                raise NotAuthorized()
            contents = req.stream.read()
            prevId = formatHash(0)
            sharedName = req.get_param('sharedName')
            if sharedName is not None:
                project.sharedName = sharedName
            if project.head is not None:
                prevId = project.head.revId
            sha1 = hashlib.sha1()
            sha1.update(prevId)
            sha1.update(contents)
            revId = sha1.hexdigest()
            revision, created = get_or_create(session, Revision, revId=revId,
                                              prevId=prevId)
            project.head = revision
            session.add(project)
            session.add(revision)
            respondXML(resp, falcon.HTTP_200, xmlSuccess({'revId': revId}))
            if created:
                revision.save(contents)


class ShareProject(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.members:
                raise NotAuthorized()
            newMember = User.fromRequest(session, req)
            project.members.append(newMember)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class ShareProjectWithStudents(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.members:
                raise NotAuthorized()
            course = Course.fromRequest(session, req)
            if user not in course.teachers:
                raise NotAuthorized()
            project.course_shared_with_students.append(course)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class ShareProjectWithTeachers(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.members:
                raise NotAuthorized()
            course = Course.fromRequest(session, req)
            if user not in course.students and user not in course.teachers:
                raise NotAuthorized()
            project.course_shared_with_teachers.append(course)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class SubmitProject(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            assignment = Assignment.fromRequest(session, req)
            project = Project.fromRequest(session, req)
            if user not in project.members:
                raise NotAuthorized()
            if user not in assignment.course.students:
                raise UserLogicError('User not enrolled in '
                                     'the class for this assignment')
            submission = Submission()
            submission.submitId = generateSubmissionId()
            submission.assignment = assignment
            submission.revision = project.head
            submission.project = project
            submission.members = project.members
            submission.submitter = user
            submission.time = datetime.datetime.utcnow()
            session.add(submission)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class UnCreateAssignment(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            assignment = Assigment.fromRequest(session, req)
            if user not in assignment.course.teachers:
                raise NotAuthorized()
            session.delete(assignment)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class UnCreateProject(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.owners:
                raise NotAuthorized()
            session.delete(project)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class UnEnroll(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            course = Course.fromRequest(session, req)
            try:
                course.students.remove(user)
            except ValueError:
                raise UserLogicError('User is not taking this course.')
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class UnMakePublic(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.members:
                raise NotAuthorized()
            project.public = False
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class UnShareProject(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.members:
                raise NotAuthorized()
            toRemove = User.fromRequest(session, req)
            if toRemove in project.owners:
                raise NotAuthorized()
            project.members.remove(toRemove)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class UnShareProjectWithStudents(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.members and user not in project.teachers:
                raise NotAuthorized()
            course = Course.fromRequest(session, req)
            if course not in project.course_shared_with_students:
                raise UserLogicError('Project not shared with students in '
                                     'this couse.')
            project.course_shared_with_students.remove(course)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class UnShareProjectWithTeachers(RootHandler):

    def on_get(self, req, resp):
        with session_scope() as session:
            user = auth(session, req, resp)
            project = Project.fromRequest(session, req)
            if user not in project.members:
                raise NotAuthorized()
            course = Course.fromRequest(session, req)
            if course not in project.course_shared_with_teachers:
                raise UserLogicError('Project not shared with teachers in '
                                     'this couse.')
            project.course_shared_with_teachers.remove(course)
            respondXML(resp, falcon.HTTP_200, xmlSuccess())


class NoMethod(RootHandler):

    def on_get(self, req, resp):
        respondXML(resp, falcon.HTTP_400, xmlError('No method in url.'))


class UnknownMethod(RootHandler):

    def on_get(self, req, resp, method):
        respondXML(resp,
                   falcon.HTTP_400,
                   xmlError('Unknown method {0!r} in url.'.format(method)))


def raise_unknown_url(req, resp):
    raise UnknownURL()


def set_access_control(req, resp, params):
    resp.set_header('Access-Control-Allow-Origin', '*')
    resp.set_header('Access-Control-Allow-Headers',
                    'Snap-Server-Authorization, Authorization')
    resp.set_header('Access-Control-Allow-Methods', 'GET, POST')
    resp.set_header('Allow', 'GET, POST')


sql_engine = sqlengine.create_engine('sqlite:///snap.sqlite', echo=False)
sql_connection = sql_engine.connect()
Session = sessionmaker(bind=sql_engine)

Base.metadata.create_all(sql_engine)

app = falcon.API(before=[set_access_control],
                 media_type='application/xml; charset=utf-8')

app.add_sink(raise_unknown_url)
app.add_route('/', NoMethod())
app.add_route('/{method}', UnknownMethod())

app.add_route('/addStudent', AddStudent())
app.add_route('/addTeacher', AddTeacher())
app.add_route('/changePassword', ChangePassword())
app.add_route('/createAssignment', CreateAssignment())
app.add_route('/createCourse', CreateCourse())
app.add_route('/createProject', CreateProject())
app.add_route('/createUser', CreateUser())
app.add_route('/enroll', Enroll())
app.add_route('/getProjectByName', GetProjectByName())
app.add_route('/getRevision', GetRevision())
app.add_route('/listAssignments', ListAssignments())
app.add_route('/listCoursesEnrolled', ListCoursesEnrolled())
app.add_route('/listCoursesTeaching', ListCoursesTeaching())
app.add_route('/listMembers', ListMembers())
app.add_route('/listProjects', ListProjects())
app.add_route('/listStudents', ListStudents())
app.add_route('/listSubmissions', ListSubmissions())
app.add_route('/listTeachers', ListTeachers())
app.add_route('/loadProject', LoadProject())
app.add_route('/makePublic', MakePublic())
app.add_route('/removeStudent', RemoveStudent())
app.add_route('/removeTeacher', RemoveTeacher())
app.add_route('/resetPassword', ResetPassword())
app.add_route('/saveProject', SaveProject())
app.add_route('/shareProject', ShareProject())
app.add_route('/shareProjectWithStudents', ShareProjectWithStudents())
app.add_route('/shareProjectWithTeachers', ShareProjectWithTeachers())
app.add_route('/submitProject', SubmitProject())
app.add_route('/uncreateAssignment', UnCreateAssignment())
app.add_route('/uncreateProject', UnCreateProject())
app.add_route('/unenroll', UnEnroll())
app.add_route('/unmakePublic', UnMakePublic())
app.add_route('/unshareProject', UnShareProject())
app.add_route('/unshareProjectWithStudents', UnShareProjectWithStudents())
app.add_route('/unshareProjectWithTeachers', UnShareProjectWithTeachers())

app.add_error_handler(Exception, handle_exception)
app.add_error_handler(ServerException, ServerException.handle_callback)


def main():
    try:
        import gevent.wsgi
        http = gevent.wsgi.WSGIServer(('', 5000), app)
    except ImportError:
        import wsgiref.simple_server
        http = wsgiref.simple_server.WSGIServer(('', 5000), app)
    http.serve_forever()


if __name__ == '__main__':
    main()
